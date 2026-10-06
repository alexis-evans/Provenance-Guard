import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from provenance_guard import create_app
from provenance_guard.scoring import classify, LABELS
from provenance_guard.stylometry import analyze_stylometry
from provenance_guard import storage
from scripts.verify_m4 import summarize


def signal(score, words=50, sentences=3):
    return {"name": "fixture", "status": "ok", "score": score,
            "details": {"word_count": words, "sentence_count": sentences}, "error_code": None}


class StylometryTests(unittest.TestCase):
    def test_hand_calculated_metrics(self):
        # Sentence lengths 2 and 4: mean 3, population SD 1; three of six words unique.
        result = analyze_stylometry("a b. a b a c!")
        metrics = result["details"]
        self.assertEqual(metrics["word_count"], 6)
        self.assertEqual(metrics["sentence_count"], 2)
        self.assertAlmostEqual(metrics["sentence_cv"], 1 / 3)
        self.assertEqual(metrics["ttr"], 0.5)
        self.assertEqual(metrics["punctuation_density"], 2 / 13)
        self.assertAlmostEqual(metrics["uniformity"], 5 / 9)
        self.assertAlmostEqual(metrics["repetition"], 2 / 3)
        self.assertAlmostEqual(result["score"], 53 / 90)

    def test_no_words_is_unavailable(self):
        for text in ["", "  ", "123 !!! 😀", "你好。"]:
            with self.subTest(text=text):
                result = analyze_stylometry(text)
                self.assertEqual(result["error_code"], "no_words")
                self.assertIsNone(result["score"])
                self.assertEqual(result["details"]["word_count"], 0)
                for key, value in result["details"].items():
                    self.assertEqual(value, 0 if key.endswith("_count") else None)

    def test_normalization_tokenization_and_sentence_fragments(self):
        text = "ＤＯＮ’T stop---now!!!\n‘WE’RE’ here? 123\nLast fragment"
        result = analyze_stylometry(text)["details"]
        self.assertEqual(result["word_count"], 7)
        self.assertEqual(result["sentence_count"], 3)
        self.assertEqual(result["ttr"], 1)

    def test_one_sentence_and_repeated_sentences(self):
        self.assertEqual(analyze_stylometry("hello hello")["details"]["sentence_cv"], 0)
        result = analyze_stylometry("one two three. " * 20)
        self.assertEqual(result["score"], 1)
        self.assertEqual(result["details"]["word_count"], 60)

    def test_ttr_uses_only_first_hundred_tokens(self):
        result = analyze_stylometry("alpha " * 100 + "beta gamma delta")
        self.assertEqual(result["details"]["word_count"], 103)
        self.assertEqual(result["details"]["ttr"], 0.01)

    def test_punctuation_is_diagnostic_only(self):
        first = analyze_stylometry("one two. three four. five six.")
        second = analyze_stylometry("one, two!!! three; four??? five: six...")
        self.assertEqual(first["score"], second["score"])
        self.assertNotEqual(first["details"]["punctuation_density"], second["details"]["punctuation_density"])

    def test_extreme_variation_clamps_and_is_deterministic(self):
        text = "one. two. " + "long " * 60
        first = analyze_stylometry(text)
        self.assertEqual(first, analyze_stylometry(text))
        self.assertEqual(first["details"]["uniformity"], 0)
        self.assertTrue(0 <= first["score"] <= 1)


class ScoringTests(unittest.TestCase):
    def test_directional_thresholds_and_confidence(self):
        for value, category in [(0, "likely_human"), (0.1999, "likely_human"),
                                (0.20, "likely_human"), (0.2001, "uncertain"),
                                (0.51, "uncertain"), (0.8999, "uncertain"),
                                (0.90, "likely_ai"), (0.9001, "likely_ai"), (1, "likely_ai")]:
            with self.subTest(value=value):
                result = classify(signal(value), signal(value))
                self.assertEqual(result["attribution"], category)
                self.assertEqual(result["ai_score"], value)
                self.assertAlmostEqual(result["confidence"], max(value, 1 - value))
                self.assertEqual(result["label"], LABELS[category])

    def test_weighting_and_same_confidence_opposite_direction(self):
        self.assertAlmostEqual(classify(signal(0.82), signal(0.94))["ai_score"], 0.868)
        for value in [0.4, 0.6]:
            self.assertEqual(classify(signal(value), signal(value))["confidence"], 0.6)

    def test_disagreement_exact_boundary_and_ordered_reasons(self):
        for g, s, expected in [(0.7999, 0.4, False), (0.8, 0.4, False),
                               (0.8001, 0.4, True), (0.81, 0.41, False)]:
            result = classify(signal(g), signal(s))
            self.assertEqual("signal_disagreement" in result["uncertainty_reasons"], expected)
        result = classify(signal(0.95), signal(0.1, words=49))
        self.assertEqual(result["uncertainty_reasons"], ["insufficient_text", "signal_disagreement", "middle_range"])
        self.assertEqual(result["ai_score"], 0.61)

    def test_length_guard_overrides_high_confidence(self):
        for words, sentences, category in [(49, 3, "uncertain"), (50, 2, "uncertain"), (50, 3, "likely_ai")]:
            result = classify(signal(0.95), signal(0.95, words, sentences))
            self.assertEqual(result["attribution"], category)
            self.assertEqual(result["confidence"], 0.95)

    def test_failed_or_invalid_signal_never_becomes_zero(self):
        invalid = [{"status": "unavailable", "score": None}]
        invalid += [signal(value) for value in [None, True, False, "0.8", -1, 2, float("nan"), float("inf")]]
        for value in invalid:
            for g, s in [(value, signal(0.95)), (signal(0.1), value)]:
                result = classify(g, s)
                self.assertEqual(result["attribution"], "uncertain")
                self.assertIsNone(result["confidence"])
                self.assertIsNone(result["ai_score"])
                self.assertEqual(result["uncertainty_reasons"][0], "signal_unavailable")

    def test_no_words_reports_both_failure_and_length(self):
        result = classify(signal(0.9), analyze_stylometry("😀"))
        self.assertEqual(result["uncertainty_reasons"], ["signal_unavailable", "insufficient_text"])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.sqlite3"

    def test_all_three_labels_via_endpoint_with_controlled_signals(self):
        # This checks wiring, not live model accuracy.
        for value, category in [(0.1, "likely_human"), (0.6, "uncertain"), (0.95, "likely_ai")]:
            app = create_app({"TESTING": True, "RATELIMIT_ENABLED": False, "DATABASE_PATH": str(self.path)}, signal=lambda text: signal(value))
            with patch("provenance_guard.analyze_stylometry", return_value=signal(value)):
                response = app.test_client().post("/submit", json={"text": "fixture", "creator_id": "test"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["attribution"], category)
            self.assertEqual(response.json["label"], LABELS[category])
            self.assertEqual(response.json["policy_version"], "ensemble-v1")

    def test_real_stylometry_runs_when_groq_fails(self):
        app = create_app({"TESTING": True, "RATELIMIT_ENABLED": False, "DATABASE_PATH": str(self.path)},
                         signal=lambda text: {"status": "unavailable", "score": None, "error_code": "timeout"})
        response = app.test_client().post("/submit", json={"text": "word " * 60 + ". Two. Three.", "creator_id": "test"})
        self.assertEqual(response.json["signals"]["stylometry"]["status"], "ok")
        self.assertIsNone(response.json["confidence"])
        self.assertEqual(response.json["uncertainty_reasons"], ["signal_unavailable"])

    def test_old_m3_event_remains_unchanged_and_new_event_has_both_scores(self):
        historical = json.loads(Path("examples/m3_verification.json").read_text())["submissions"][0]["response"]
        app = create_app({"TESTING": True, "RATELIMIT_ENABLED": False, "DATABASE_PATH": str(self.path)}, signal=lambda text: signal(0.95))
        storage.save_decision(self.path, historical)
        before = storage.read_events(self.path, 100, 0)[0]
        response = app.test_client().post("/submit", json={"text": "one two three. " * 20, "creator_id": "test"})
        events = app.test_client().get("/log").json["events"]
        self.assertEqual(events[0], before)
        self.assertEqual(response.json["attribution"], "likely_ai")
        self.assertEqual(response.json["ai_score"], 0.9225)
        for key, value in response.json.items():
            self.assertEqual(events[1][key], value)


class EvaluationReportTests(unittest.TestCase):
    def test_repeats_and_failures_do_not_inflate_known_origin_denominators(self):
        def entry(origin, category, repeat=1, status="ok"):
            return {"case": {"origin": origin}, "repeat": repeat,
                    "response": {"attribution": category, "signals": {"groq": {"status": status}}}}
        result = summarize([
            entry("human", "likely_ai"), entry("human", "likely_ai", repeat=2),
            entry("human", "uncertain", status="unavailable"),
            entry("ai", "likely_human"), entry("unknown", "uncertain"),
        ])
        self.assertEqual(result["unique_inputs"], 4)
        self.assertEqual(result["requests"], 5)
        self.assertEqual(result["known_human_inputs"], 1)
        self.assertEqual(result["human_false_positives"], 1)
        self.assertEqual(result["known_ai_inputs"], 1)
        self.assertEqual(result["ai_false_negatives"], 1)
        self.assertEqual(result["uncertain_fraction"], 0.5)
        self.assertEqual(result["provider_failures"], 1)


if __name__ == "__main__":
    unittest.main()
