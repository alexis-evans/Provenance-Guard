"""Deterministic English-oriented structural signal specified in planning.md."""

import re
from statistics import mean, pstdev
import unicodedata


WORD = re.compile(r"[a-z]+(?:'[a-z]+)*")
SENTENCE_END = re.compile(r"[.!?\n]+")


def clamp(value):
    return min(1.0, max(0.0, value))


def analyze_stylometry(text):
    normalized = unicodedata.normalize("NFKC", text).replace("’", "'").replace("‘", "'").lower()
    words = WORD.findall(normalized)
    lengths = [len(WORD.findall(part)) for part in SENTENCE_END.split(normalized)]
    lengths = [length for length in lengths if length]
    details = {
        "word_count": len(words),
        "sentence_count": len(lengths),
        "sentence_cv": None,
        "ttr": None,
        "punctuation_density": None,
        "uniformity": None,
        "repetition": None,
    }
    if not words:
        return {
            "name": "stylometry", "status": "unavailable", "score": None,
            "details": details, "error_code": "no_words",
        }
    cv = pstdev(lengths) / mean(lengths)
    window = words[:100]
    ttr = len(set(window)) / len(window)
    uniformity = 1 - clamp(cv / 0.75)
    repetition = 1 - clamp((ttr - 0.35) / 0.45)
    details.update(
        sentence_cv=cv,
        ttr=ttr,
        punctuation_density=sum(unicodedata.category(c).startswith("P") for c in normalized) / len(normalized),
        uniformity=uniformity,
        repetition=repetition,
    )
    return {
        "name": "stylometry", "status": "ok",
        "score": 0.70 * uniformity + 0.30 * repetition,
        "details": details, "error_code": None,
    }
