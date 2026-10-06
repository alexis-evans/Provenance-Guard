"""Verify real HTTP rate limiting; add --live for three Groq submissions and an appeal."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from provenance_guard import create_app, ROOT, SUBMISSION_LIMITS
from provenance_guard.scoring import LABELS


@contextmanager
def serve(app):
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def request(base, path, body=None):
    req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                  headers={"Content-Type": "application/json"})
    try:
        response = urlopen(req, timeout=25)
    except HTTPError as error:
        response = error
    with response:
        return {"status": response.status, "body": json.load(response),
                "retry_after": response.headers.get("Retry-After")}


def write_report(name, data):
    (ROOT / "instance").mkdir(exist_ok=True)
    (ROOT / "instance" / name).write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")


def verify():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Send three public examples to Groq and persist an appeal")
    args = parser.parse_args()
    inputs = json.loads((ROOT / "examples/m4_inputs.json").read_text())[:3]
    now = lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Independent app and temporary DB: burst verification consumes no Groq quota.
    burst_inputs = [dict(item) for item in inputs]
    burst_inputs[0]["text"] = "one two three. " * 20
    calls = []
    def controlled_signal(text):
        calls.append(text)
        score = 0.95 if text == burst_inputs[0]["text"] else 0.0
        return {"name": "groq", "status": "ok", "score": score, "error_code": None,
                "details": {"model": "controlled-fixture-not-live", "rationale": "Deterministic fixture for label and limiter verification."}}

    with TemporaryDirectory() as directory:
        controlled = create_app({"DATABASE_PATH": str(Path(directory) / "burst.sqlite3")}, signal=controlled_signal)
        with serve(controlled) as base:
            responses = [request(base, "/submit", {"text": burst_inputs[i % 3]["text"], "creator_id": "rate-limit-test"}) for i in range(12)]
            statuses = [r["status"] for r in responses]
            assert statuses == [200] * 10 + [429, 429], statuses
            assert len(calls) == 10
            assert {r["body"]["attribution"] for r in responses[:3]} == set(LABELS)
            for r in responses[:3]:
                assert r["body"]["label"] == LABELS[r["body"]["attribution"]]
            for r in responses[10:]:
                assert r["body"]["error"]["code"] == "rate_limited"
                assert int(r["retry_after"]) > 0
            log = request(base, "/log")
            assert log["status"] == 200 and len(log["body"]["events"]) == 10
        rate_report = {
            "verified_at": now(), "transport": "Real loopback HTTP; real Flask-Limiter; real temporary SQLite",
            "detector": "Controlled Groq fixture; real stylometry. No provider calls.",
            "limits": SUBMISSION_LIMITS, "key": "request.remote_addr", "storage": "memory://", "strategy": "fixed-window",
            "status_codes": statuses, "blocked_responses": responses[10:],
            "provider_adapter_invocations": len(calls), "decision_audit_events": 10,
            "label_checks": [r["body"] for r in responses[:3]],
        }
        write_report("m5_rate_limit_evidence.json", rate_report)
        print("Rate-limit HTTP evidence:", " ".join(map(str, statuses)), flush=True)
    if not args.live:
        print("Controlled verification complete. Add --live to verify actual Groq submissions and an appeal.")
        return

    app = create_app()
    try:
        with serve(app) as base:
            decisions = []
            for case in inputs:
                response = request(base, "/submit", {"text": case["text"], "creator_id": "m5-demo-" + case["name"]})
                assert response["status"] == 200, response["status"]
                decision = response["body"]
                assert decision["signals"]["groq"]["status"] == "ok", decision["signals"]["groq"]["error_code"]
                assert decision["label"] == LABELS[decision["attribution"]]
                decisions.append(decision)
                print(f"Live submission: {case['name']} -> {decision['attribution']}", flush=True)
            target = decisions[-1]["content_id"]
            before = request(base, "/content/" + target)
            assert before["status"] == 200 and before["body"]["status"] == "classified"
            appeal_body = {
                "content_id": target,
                "creator_reasoning": "This is a public-domain passage by Charles Darwin, not generated prose. Please review the historical source and the effect of formal writing on the structural signal.",
            }
            appeal = request(base, "/appeal", appeal_body)
            assert appeal["status"] == 201
            after = request(base, "/content/" + target)
            assert after["status"] == 200 and after["body"]["status"] == "under_review"
            assert after["body"]["appeal"]["creator_reasoning"] == appeal_body["creator_reasoning"]
            assert after["body"]["appeal"]["appeal_id"] == appeal["body"]["appeal_id"]
            duplicate = request(base, "/appeal", appeal_body)
            assert duplicate["status"] == 409
            ids = {d["content_id"] for d in decisions}
            events, offset = [], 0
            while True:
                page = request(base, f"/log?limit=100&offset={offset}")["body"]["events"]
                events.extend(e for e in page if e["content_id"] in ids)
                if len(page) < 100:
                    break
                offset += 100
            assert len(events) == 4
            for decision in decisions:
                event = next(e for e in events if e["content_id"] == decision["content_id"] and e["event_type"] == "decision")
                assert all(event[k] == v for k, v in decision.items())
            original = next(e for e in events if e["content_id"] == target and e["event_type"] == "decision")
            appealed = next(e for e in events if e["event_type"] == "appeal")
            assert appealed["original_decision_event_id"] == original["event_id"]
            for field in ["ai_score", "confidence", "signals", "attribution", "label"]:
                assert before["body"][field] == after["body"][field] == appealed[field]
            write_report("m5_verification.json", {
                "verified_at": now(), "transport": "Real loopback HTTP and live Groq",
                "model": app.config["GROQ_MODEL"], "inputs": "First three cases in examples/m4_inputs.json",
                "submissions": decisions, "appeal_response": appeal, "duplicate_response": duplicate,
                "content_before": before["body"], "content_after": after["body"], "audit_events": events,
            })
            print("Verified: three live decisions, an appeal, under-review content, duplicate rejection, and four linked audit events.")
    finally:
        app.extensions["groq_client"].close()


if __name__ == "__main__":
    verify()
