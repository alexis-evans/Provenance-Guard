"""Opt-in live verification: six Groq assessments and three persisted decisions.

Run from the project root: python -m scripts.verify_m3 --live
Only the public course examples in examples/m3_inputs.json are submitted.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from urllib.request import Request, urlopen

from groq import APIError
from werkzeug.serving import make_server

from provenance_guard import create_app, ROOT


def verify():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow six live Groq assessments")
    parser.add_argument("--output", type=Path, default=ROOT / "instance/m3_fixture_rerun.json")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to send the course examples to Groq and consume API quota.")
    if args.output.resolve() == (ROOT / "examples/m3_verification.json").resolve():
        parser.error("Preserve the historical M3 report; choose a different output path.")

    app = create_app()
    sdk = app.extensions["groq_client"]
    try:
        model = app.config["GROQ_MODEL"]
        available = {item.id for item in sdk.models.list().data}
        if model not in available:
            raise RuntimeError("Configured GROQ_MODEL is absent from the account model list.")
        cases = json.loads((ROOT / "examples/m3_inputs.json").read_text())
        direct = []
        for case in cases:
            result = app.extensions["groq_signal"](case["text"])
            if result["status"] != "ok":
                raise RuntimeError(f"Direct adapter check failed: {result['error_code']}")
            direct.append({"name": case["name"], "signal": result})
            print(f"Direct adapter: {case['name']} -> {result['score']}", flush=True)

        # Use a real loopback HTTP server, not a mock or Flask's test client.
        server = make_server("127.0.0.1", 0, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        submissions = []
        try:
            for case in cases:
                payload = {"text": case["text"], "creator_id": f"m3-demo-{case['name']}"}
                request = Request(base_url + "/submit", data=json.dumps(payload).encode(),
                                  headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=25) as response:
                    status, decision = response.status, json.load(response)
                if status != 200 or decision["signals"]["groq"]["status"] != "ok":
                    raise RuntimeError("Live HTTP submission did not produce a successful Groq signal.")
                submissions.append({"name": case["name"], "http_status": status, "response": decision})
                print(f"POST /submit: {case['name']} -> HTTP {status}, score {decision['signals']['groq']['score']}", flush=True)

            ids = {entry["response"]["content_id"] for entry in submissions}
            events, offset = [], 0
            while True:
                with urlopen(base_url + f"/log?limit=100&offset={offset}", timeout=10) as response:
                    page = json.load(response)["events"]
                events.extend(event for event in page if event["content_id"] in ids)
                if len(page) < 100:
                    break
                offset += 100
            if len(events) != len(submissions):
                raise RuntimeError("Audit event count does not match submissions.")
            for entry in submissions:
                decision = entry["response"]
                event = next(e for e in events if e["content_id"] == decision["content_id"])
                if any(event.get(key) != value for key, value in decision.items()):
                    raise RuntimeError("Audit event differs from returned decision.")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        report = {
            "verified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "model": model,
            "model_list_verified": True,
            "transport": "Live Groq API and real loopback HTTP Flask server",
            "input_source": "examples/m3_inputs.json; illustrative samples, not an accuracy benchmark",
            "policy_version": submissions[0]["response"]["policy_version"],
            "direct_adapter_checks": direct,
            "submissions": submissions,
            "audit_events": events,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        print(f"Verified {len(events)} durable audit events. Report: {args.output}")
    finally:
        sdk.close()


if __name__ == "__main__":
    try:
        verify()
    except APIError as exc:
        # Provider exception text may include request details. Report only type/status.
        raise SystemExit(f"Groq verification failed: {type(exc).__name__}; HTTP {getattr(exc, 'status_code', None)}") from None
