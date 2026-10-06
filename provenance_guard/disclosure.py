"""A lexical origin-disclosure signal, separate from style and model judgment."""
import re

CUES = {
    "ai_identity": r"\bas an (?:ai|artificial intelligence)(?: language model| assistant)?\b",
    "generated_by": r"\b(?:generated|written|produced) by (?:chatgpt|an ai|an artificial intelligence|a language model)\b",
}


def analyze_disclosure(text):
    matches = [name for name, pattern in CUES.items() if re.search(pattern, text, re.I)]
    return {"name": "disclosure", "status": "ok", "score": 1.0 if matches else 0.5,
            "details": {"matched_cues": matches, "version": "disclosure-v1",
                        "meaning": "Explicit AI-origin wording" if matches else "No origin evidence"},
            "error_code": None}
