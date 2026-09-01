"""
Probability Engine (spec sections 7, 8, 10).

V1 rule from the spec: probability must NOT be an arbitrary score
("score 85 = probability 85%"). Instead:

  1. Each factor contributes a small, clearly-labeled amount ("contribution").
  2. Contributions are summed into a raw score.
  3. The raw score is squashed into a 0-100% range.
  4. `reasons()` explains, in plain language, why the score is what it is
     (spec section 15: AI EXPLANATION).

Once real historical outcomes exist in SignalLog (see models.py), a
separate calibration step (spec section 10) should adjust these raw
percentages so that "70-75% predicted" buckets actually win about
70-75% of the time. That calibration is NOT implemented yet - this
file only produces the raw, uncalibrated estimate. A TODO marks where
calibration should be plugged in.
"""

from dataclasses import dataclass


@dataclass
class ProbabilityResult:
    probability: float
    reasons: list[str]


def _clip(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def score_stock(snapshot: dict) -> ProbabilityResult:
    """snapshot comes from mock_data.generate_stock_snapshot (or, later, real data)."""
    contribution = 50.0  # neutral baseline
    reasons = []

    if snapshot["price_vs_ema20"] == "above":
        contribution += 8
        reasons.append("Price above EMA 20")
    else:
        contribution -= 8
        reasons.append("Price below EMA 20")

    if snapshot["ema20_vs_ema50"] == "above":
        contribution += 6
        reasons.append("EMA 20 above EMA 50")
    else:
        contribution -= 6
        reasons.append("EMA 20 below EMA 50")

    if snapshot["price_vs_vwap"] == "above":
        contribution += 5
        reasons.append("Price above VWAP")
    else:
        contribution -= 5
        reasons.append("Price below VWAP")

    rsi = snapshot["rsi"]
    if 50 <= rsi <= 70:
        contribution += 7
        reasons.append(f"RSI in bullish zone ({rsi})")
    elif rsi > 70:
        contribution += 2
        reasons.append(f"RSI overbought ({rsi}) - momentum strong but stretched")
    elif rsi < 30:
        contribution -= 7
        reasons.append(f"RSI oversold ({rsi})")

    if snapshot["change_pct"] > 0:
        contribution += min(snapshot["change_pct"] * 2, 10)
        reasons.append(f"Positive momentum ({snapshot['change_pct']}%)")
    else:
        contribution += max(snapshot["change_pct"] * 2, -10)
        reasons.append(f"Negative momentum ({snapshot['change_pct']}%)")

    if snapshot["volume"] > 5_000_000:
        contribution += 4
        reasons.append("Volume above average")

    # TODO: once SignalLog has enough closed trades, replace this straight
    # clip with a calibration lookup (spec section 10, probability buckets).
    return ProbabilityResult(probability=round(_clip(contribution), 1), reasons=reasons)


def score_option(direction: str, oi_change_pct: float, iv: float, underlying_probability: float) -> ProbabilityResult:
    """A simplified option-side adjustment layered on top of the underlying's probability."""
    contribution = underlying_probability
    reasons = [f"Underlying {direction.lower()} probability: {underlying_probability}%"]

    if direction == "BULLISH" and oi_change_pct > 0:
        contribution += 5
        reasons.append("Call OI building up (supportive)")
    elif direction == "BEARISH" and oi_change_pct > 0:
        contribution += 5
        reasons.append("Put OI building up (supportive)")

    if iv > 20:
        contribution -= 3
        reasons.append(f"IV relatively high ({iv}) - option pricier")
    else:
        reasons.append(f"IV acceptable ({iv})")

    return ProbabilityResult(probability=round(_clip(contribution), 1), reasons=reasons)
