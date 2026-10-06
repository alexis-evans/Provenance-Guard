"""Versioned detection policies. Heuristic confidence is not a probability."""

from decimal import Decimal
import math


POLICY_VERSION = "ensemble-v1"
LABEL_VERSION = "transparency-v1"
LABELS = {
    "likely_ai": "This text shows strong signs of AI generation. This automated assessment can be wrong. Creators can appeal.",
    "likely_human": "This text shows strong signs of human writing. This automated assessment does not verify authorship. Creators can appeal.",
    "uncertain": "We cannot confidently determine whether this text is human-written or AI-generated. Creators can appeal.",
}


def available(signal):
    value = signal.get("score")
    return (signal.get("status") == "ok" and type(value) in (int, float)
            and 0 <= value <= 1 and math.isfinite(value))


def classify(groq, stylometry):
    """Keep diagnostics in the caller; return final score, guards, and label."""
    reasons = []
    complete = available(groq) and available(stylometry)
    if not complete:
        reasons.append("signal_unavailable")
    metrics = stylometry.get("details", {})
    if metrics.get("word_count", 0) < 50 or metrics.get("sentence_count", 0) < 3:
        reasons.append("insufficient_text")

    attribution = "uncertain"
    ai_score = confidence = None
    if complete:
        g, s = Decimal(str(groq["score"])), Decimal(str(stylometry["score"]))
        combined = Decimal("0.60") * g + Decimal("0.40") * s
        ai_score = float(combined)
        confidence = float(max(combined, 1 - combined))
        if abs(g - s) > Decimal("0.40"):
            reasons.append("signal_disagreement")
        if Decimal("0.20") < combined < Decimal("0.90"):
            reasons.append("middle_range")
        if not reasons:
            attribution = "likely_ai" if combined >= Decimal("0.90") else "likely_human"
    return {
        "attribution": attribution,
        "ai_score": ai_score,
        "confidence": confidence,
        "uncertainty_reasons": reasons,
        "label": LABELS[attribution],
        "policy_version": "heuristic-v1",
        "label_version": LABEL_VERSION,
    }


def classify_ensemble(groq, stylometry, disclosure):
    """Use three weighted signals, with neutral disclosure excluded from conflict."""
    result = classify(groq, stylometry)
    reasons = [r for r in result["uncertainty_reasons"]
               if r not in ("middle_range", "signal_disagreement", "signal_unavailable")]
    weights = {"groq": 0.55, "stylometry": 0.35, "disclosure": 0.10}
    signals = dict(groq=groq, stylometry=stylometry, disclosure=disclosure)
    score = confidence = None
    attribution = "uncertain"
    if not all(available(s) for s in signals.values()):
        reasons.append("signal_unavailable")
    else:
        values = {k: Decimal(str(s["score"])) for k, s in signals.items()}
        combined = sum(Decimal(str(weights[k])) * v for k, v in values.items())
        informative = [values["groq"], values["stylometry"]]
        if disclosure.get("details", {}).get("matched_cues"):
            informative.append(values["disclosure"])
        if max(informative) - min(informative) > Decimal("0.40"):
            reasons.append("signal_disagreement")
        if Decimal("0.20") < combined < Decimal("0.90"):
            reasons.append("middle_range")
        score, confidence = float(combined), float(max(combined, 1 - combined))
        if not reasons:
            attribution = "likely_ai" if combined >= Decimal("0.90") else "likely_human"
    return dict(result, attribution=attribution, ai_score=score, confidence=confidence,
                uncertainty_reasons=reasons, label=LABELS[attribution],
                policy_version=POLICY_VERSION, weights=weights)
