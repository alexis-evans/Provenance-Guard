"""Evaluate M4 metrics offline, or opt in to real HTTP and Groq calls with --live.

Default live set: 20 assessments (four development cases repeated three times,
four evaluation cases, four course examples). --user-inputs adds local samples.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
import time
from urllib.request import Request, urlopen

from groq import APIError
from werkzeug.serving import make_server

from provenance_guard import create_app, ROOT
from provenance_guard.scoring import POLICY_VERSION, LABEL_VERSION
from provenance_guard.stylometry import analyze_stylometry


def summarize(entries):
    """Per-input counts use only first runs so stability repeats don't inflate n."""
    first = [entry for entry in entries if entry["repeat"] == 1]
    assessed = [entry for entry in first if entry["response"]["signals"]["groq"]["status"] == "ok"]
    humans = [entry for entry in assessed if entry["case"]["origin"] == "human"]
    generated = [entry for entry in assessed if entry["case"]["origin"] == "ai"]
    return {
        "unique_inputs": len(first),
        "requests": len(entries),
        "uncertain_inputs": sum(e["response"]["attribution"] == "uncertain" for e in first),
        "uncertain_fraction": sum(e["response"]["attribution"] == "uncertain" for e in first) / len(first) if first else None,
        "known_human_inputs": len(humans),
        "human_false_positives": sum(e["response"]["attribution"] == "likely_ai" for e in humans),
        "known_ai_inputs": len(generated),
        "ai_false_negatives": sum(e["response"]["attribution"] == "likely_human" for e in generated),
        "provider_failures": sum(e["response"]["signals"]["groq"]["status"] != "ok" for e in entries),
    }


def verify():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Reuse successful public checks from the output report; rerun failures and local inputs")
    parser.add_argument("--interval", type=float, default=6.0, help="Seconds between live requests (default 6; maximum 60)")
    parser.add_argument("--user-inputs", type=Path, help="Optional ignored local JSON samples; text and rationale excluded from public report")
    parser.add_argument("--output", type=Path, default=ROOT / "instance/m4_current_verification.json")
    args = parser.parse_args()
    if args.output.resolve() == (ROOT / "examples/m4_verification.json").resolve():
        parser.error("Preserve the historical M4 report; choose a different output path.")
    if not 0 <= args.interval <= 60:
        parser.error("interval must be between 0 and 60 seconds")
    cases = json.loads((ROOT / "examples/m4_inputs.json").read_text())
    if args.user_inputs:
        private = json.loads(args.user_inputs.read_text())
        if any(c["split"] != "user_short" for c in private):
            parser.error("Local samples must use split user_short to ensure report redaction.")
        cases.extend(private)
    for case in cases:
        measured = analyze_stylometry(case["text"])
        print(f"{case['name']}: words={measured['details']['word_count']}, sentences={measured['details']['sentence_count']}, structural_score={measured['score']}", flush=True)
    if not args.live:
        print("Offline metrics only. Use --live for HTTP submissions and Groq evaluation.")
        return

    app = create_app()
    sdk = app.extensions["groq_client"]
    server = thread = None
    try:
        if app.config["GROQ_MODEL"] not in {m.id for m in sdk.models.list().data}:
            raise RuntimeError("Configured model is not in the account's model list.")
        server = make_server("127.0.0.1", 0, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        previous = {}
        if args.resume:
            saved = json.loads(args.output.read_text())
            if saved["policy_version"] != POLICY_VERSION or saved["model"] != app.config["GROQ_MODEL"]:
                raise RuntimeError("Cannot resume a report from a different model or policy.")
            previous = {(e["case"]["name"], e["repeat"]): e for e in saved["submissions"]
                        if e["response"]["signals"]["groq"]["status"] == "ok"}
        entries = []
        for case in cases:
            for repeat in range(1, (3 if case["split"] == "development" else 1) + 1):
                prior = previous.get((case["name"], repeat))
                if prior is not None:
                    if (prior["response"].get("label_version") != LABEL_VERSION
                            or prior.get("input_sha256") != hashlib.sha256(case["text"].encode()).hexdigest()
                            or prior["case"] != {k: v for k, v in case.items() if k != "text"}
                            or prior["response"]["signals"]["stylometry"] != analyze_stylometry(case["text"])):
                        raise RuntimeError("Input, provenance, or metrics changed; rerun without --resume.")
                    entries.append(prior)
                    continue
                time.sleep(args.interval)
                payload = {"text": case["text"], "creator_id": "m4-demo-" + case["name"]}
                req = Request(base + "/submit", data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=25) as response:
                    status, decision = response.status, json.load(response)
                if status != 200 or decision["policy_version"] != POLICY_VERSION:
                    raise RuntimeError("Unexpected response or policy version.")
                if decision["signals"]["stylometry"] != analyze_stylometry(case["text"]):
                    raise RuntimeError("Endpoint structural metrics differ from the standalone function.")
                entries.append({"case": {k: v for k, v in case.items() if k != "text"},
                                "input_sha256": hashlib.sha256(case["text"].encode()).hexdigest(),
                                "repeat": repeat, "http_status": status, "response": decision})
                print(f"{case['name']} #{repeat}: groq={decision['signals']['groq']['score']}, ai_score={decision['ai_score']}, confidence={decision['confidence']}, {decision['attribution']} {decision['uncertainty_reasons']}", flush=True)

        ids = {entry["response"]["content_id"] for entry in entries}
        events, offset = [], 0
        while True:
            with urlopen(base + f"/log?limit=100&offset={offset}", timeout=10) as response:
                page = json.load(response)["events"]
            events.extend(e for e in page if e["content_id"] in ids)
            if len(page) < 100:
                break
            offset += 100
        if len(events) != len(entries):
            raise RuntimeError("Audit event count differs from submissions.")
        for entry in entries:
            decision = entry["response"]
            event = next(e for e in events if e["content_id"] == decision["content_id"])
            if any(event.get(key) != value for key, value in decision.items()):
                raise RuntimeError("Audit event does not match the API response.")

        public = [e for e in entries if e["case"]["split"] != "user_short"]
        public_ids = {e["response"]["content_id"] for e in public}
        user_checks = []
        for entry in entries:
            if entry["case"]["split"] != "user_short":
                continue
            decision = entry["response"]
            user_checks.append({
                "name": entry["case"]["name"], "source": entry["case"]["source"],
                "word_count": decision["signals"]["stylometry"]["details"]["word_count"],
                "sentence_count": decision["signals"]["stylometry"]["details"]["sentence_count"],
                "groq_score": decision["signals"]["groq"]["score"],
                "stylometry_score": decision["signals"]["stylometry"]["score"],
                **{k: decision[k] for k in ["attribution", "ai_score", "confidence", "uncertainty_reasons", "label"]},
                "audit_match_verified": True,
            })
        report = {
            "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "model": app.config["GROQ_MODEL"], "policy_version": POLICY_VERSION,
            "interval_seconds": args.interval,
            "resumed_successful_checks": len(previous),
            "transport": "Live Groq API and real loopback HTTP server",
            "limitations": "Tiny curated sample. Classic human texts may be recognized. Edited examples are AI rewrites, not human-edited ground truth. Confidence is uncalibrated.",
            "summary_by_split": {group: summarize([e for e in public if e["case"]["split"] == group])
                                 for group in ["development", "evaluation", "course_short"]},
            "submissions": public,
            "audit_events": [e for e in events if e["content_id"] in public_ids],
            "user_short_checks": user_checks,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        print(f"Verified {len(events)} audit matches. Saved report: {args.output}")
        if any(e["response"]["signals"]["groq"]["status"] != "ok" for e in entries):
            raise SystemExit("Report saved, but provider failures occurred; do not treat them as successful live assessments.")
    finally:
        if thread is not None:
            server.shutdown()
            thread.join(timeout=5)
        if server is not None:
            server.server_close()
        sdk.close()


if __name__ == "__main__":
    try:
        verify()
    except APIError as exc:
        raise SystemExit(f"Groq verification failed: {type(exc).__name__}; HTTP {getattr(exc, 'status_code', None)}") from None
