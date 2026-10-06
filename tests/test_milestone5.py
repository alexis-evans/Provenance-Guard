from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from provenance_guard import create_app
from provenance_guard.scoring import LABELS
from provenance_guard import storage


def fake_signal(text):
    return {"name": "groq", "status": "ok", "score": 0.95,
            "details": {"model": "offline-test", "rationale": "Controlled fixture"}, "error_code": None}


class ProductionLayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.signal = Mock(side_effect=fake_signal)
        self.app = create_app({"TESTING": True, "DATABASE_PATH": str(self.path)}, signal=self.signal)
        self.client = self.app.test_client()
        self.body = {"text": "one two three. " * 20, "creator_id": "test"}

    def submit(self):
        response = self.client.post("/submit", json=self.body)
        self.assertEqual(response.status_code, 200)
        return response.json

    def appeal(self, content_id, reasoning="  I wrote this myself and can describe my drafts.  "):
        return self.client.post("/appeal", json={"content_id": content_id, "creator_reasoning": reasoning})

    def test_appeal_preserves_original_and_survives_restart(self):
        decision = self.submit()
        before = self.client.get("/log").json["events"][0]
        original = self.client.get("/content/" + decision["content_id"]).json
        self.assertIsNone(original["appeal"])
        self.assertIsNone(original["review_notice"])
        self.assertEqual(original["updated_at"], original["created_at"])
        result = self.appeal(decision["content_id"])
        self.assertEqual(result.status_code, 201)
        UUID(result.json["appeal_id"])
        events = self.client.get("/log").json["events"]
        self.assertEqual(events[0], before)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["original_decision_event_id"], before["event_id"])
        self.assertEqual(events[1]["appeal_reasoning"], "I wrote this myself and can describe my drafts.")
        self.assertEqual(events[1]["status"], "under_review")
        for field in ["attribution", "confidence", "ai_score", "signals", "label", "created_at"]:
            self.assertEqual(events[1][field], decision[field])
        restarted = create_app({"TESTING": True, "DATABASE_PATH": str(self.path)}, signal=self.signal)
        content = restarted.test_client().get("/content/" + decision["content_id"]).json
        self.assertEqual(content["status"], "under_review")
        self.assertEqual(content["review_notice"], storage.REVIEW_NOTICE)
        self.assertEqual(content["appeal"]["appeal_id"], result.json["appeal_id"])
        self.assertEqual(content["updated_at"], result.json["created_at"])
        self.signal.assert_called_once()

    def test_duplicate_and_concurrent_appeals(self):
        decision = self.submit()
        barrier = threading.Barrier(2)
        def send():
            with self.app.test_client() as client:
                barrier.wait(timeout=5)
                return client.post("/appeal", json={"content_id": decision["content_id"], "creator_reasoning": "My own work."}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: send(), range(2)))
        self.assertEqual(sorted(statuses), [201, 409])
        again = self.appeal(decision["content_id"])
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json["error"]["code"], "appeal_exists")
        self.assertEqual(len(self.client.get("/log").json["events"]), 2)

    def test_appeal_validation_and_missing_content(self):
        decision = self.submit()
        for value in [None, False, 7, "", "  ", "x" * 5001]:
            self.assertEqual(self.appeal(decision["content_id"], value).status_code, 400)
        for content_id in [None, 7, {}, "bad-uuid"]:
            self.assertEqual(self.appeal(content_id).status_code, 400)
        for body in [None, [], {}, {"content_id": decision["content_id"]},
                     {"content_id": decision["content_id"], "creator_reasoning": "yes", "extra": 1}]:
            self.assertEqual(self.client.post("/appeal", data=json.dumps(body), content_type="application/json").status_code, 400)
        self.assertEqual(self.appeal(str(uuid4())).status_code, 404)
        self.assertEqual(self.client.post("/appeal", data="{", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post("/appeal", data="text").status_code, 415)
        self.assertEqual(self.client.post("/appeal", data=" " * 131073, content_type="application/json").status_code, 413)
        self.assertEqual(self.client.get("/content/invalid").status_code, 404)
        self.assertEqual(self.client.get("/content/" + str(uuid4())).status_code, 404)
        self.assertEqual(len(self.client.get("/log").json["events"]), 1)
        self.assertEqual(self.client.get("/content/" + decision["content_id"]).json["status"], "classified")
        # Maximum accepted length, and canonical UUID lookup.
        self.assertEqual(self.appeal(decision["content_id"].upper(), " " + "x" * 5000 + " ").status_code, 201)

    def test_audit_failure_rolls_back_entire_appeal(self):
        decision = self.submit()
        with storage.connect(self.path) as db:
            db.execute("""CREATE TRIGGER fail_appeal BEFORE INSERT ON audit_events
                          WHEN NEW.event_type = 'appeal'
                          BEGIN SELECT RAISE(ABORT, 'forced failure'); END""")
        response = self.appeal(decision["content_id"])
        self.assertEqual(response.status_code, 503)
        content = self.client.get("/content/" + decision["content_id"]).json
        self.assertEqual(content["status"], "classified")
        self.assertIsNone(content["appeal"])
        self.assertEqual(content["updated_at"], decision["created_at"])
        self.assertEqual(len(self.client.get("/log").json["events"]), 1)

    def test_legacy_decision_is_appealable_without_rewriting_label(self):
        historical = json.loads(Path("examples/m3_verification.json").read_text())["submissions"][0]["response"]
        storage.save_decision(self.path, historical)
        self.assertEqual(self.appeal(historical["content_id"]).status_code, 201)
        content = self.client.get("/content/" + historical["content_id"]).json
        self.assertEqual(content["label"], historical["label"])
        self.assertIsNone(content["confidence"])
        self.assertEqual(content["policy_version"], "m3-single-signal")

    def test_uncertain_and_human_decisions_can_also_be_appealed(self):
        for value, category in [(0.1, "likely_human"), (0.6, "uncertain")]:
            self.signal.side_effect = lambda text: {**fake_signal(text), "score": value}
            structural = {"status": "ok", "score": value, "details": {"word_count": 60, "sentence_count": 3}}
            with patch("provenance_guard.analyze_stylometry", return_value=structural):
                decision = self.submit()
            self.assertEqual(decision["attribution"], category)
            self.assertEqual(self.appeal(decision["content_id"]).status_code, 201)

    def test_final_labels_match_the_written_spec(self):
        planning, readme = Path("planning.md").read_text(), Path("README.md").read_text()
        for label in LABELS.values():
            self.assertTrue(label.endswith("Creators can appeal."))
            self.assertIn(label, planning)
            self.assertIn(label, readme)
        self.assertEqual(self.submit()["label_version"], "transparency-v1")

    def test_minute_limit_blocks_before_provider_and_keeps_other_routes_open(self):
        responses = [self.client.post("/submit", json=self.body) for _ in range(12)]
        self.assertEqual([r.status_code for r in responses], [200] * 10 + [429, 429])
        self.assertEqual(responses[-1].json["error"]["code"], "rate_limited")
        self.assertGreater(int(responses[-1].headers["Retry-After"]), 0)
        self.assertEqual(self.signal.call_count, 10)
        self.assertEqual(len(self.client.get("/log").json["events"]), 10)
        content_id = responses[0].json["content_id"]
        self.assertEqual(self.client.get("/content/" + content_id).status_code, 200)
        self.assertEqual(self.appeal(content_id).status_code, 201)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_ip_key_ignores_creator_id_and_forwarded_header(self):
        for _ in range(10):
            self.client.post("/submit", json=self.body)
        denied = self.client.post("/submit", json={**self.body, "creator_id": "different"}, headers={"X-Forwarded-For": "203.0.113.42"})
        self.assertEqual(denied.status_code, 429)
        independent = self.client.post("/submit", json=self.body, environ_overrides={"REMOTE_ADDR": "203.0.113.42"})
        self.assertEqual(independent.status_code, 200)

    def test_invalid_attempts_consume_quota(self):
        for _ in range(10):
            self.assertEqual(self.client.post("/submit", json={}).status_code, 400)
        self.assertEqual(self.client.post("/submit", json=self.body).status_code, 429)
        self.signal.assert_not_called()

    def test_daily_limit_and_window_reset_with_controlled_clock(self):
        # The memory store and limiter use time.time; no sleeping or provider calls.
        with patch("time.time", return_value=2_000_000_000.0) as clock:
            for minute in range(10):
                clock.return_value = 2_000_000_000.0 + minute * 61
                for _ in range(10):
                    self.assertEqual(self.client.post("/submit", json=self.body).status_code, 200)
            clock.return_value += 61
            denied = self.client.post("/submit", json=self.body)
            self.assertEqual(denied.status_code, 429)
            self.assertGreater(int(denied.headers["Retry-After"]), 80_000)
            self.assertEqual(self.signal.call_count, 100)
            clock.return_value = 2_000_000_000.0 + 86401
            self.assertEqual(self.client.post("/submit", json=self.body).status_code, 200)


if __name__ == "__main__":
    unittest.main()
