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

from . import strategies


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

    # Strategy confluence (strategies.py): each named setup (EMA 9/21,
    # EMA 20/50, EMA 50/200, RSI+EMA 20&50, VWAP+EMA, MACD+Bollinger,
    # Parabolic SAR+Stochastic RSI, RSI+ADX, Pivot Points) only fires when
    # ALL of its own conditions line up - see strategies.py's own gating.
    # Net agreement across those independent setups is a small nudge here,
    # not a separate probability of its own, and it's capped so a handful
    # of aligned setups can't swamp the indicator-level scoring above.
    strategy_results = strategies.run_all_strategies(snapshot)
    net_strategy_signal = len(strategy_results["bullish"]) - len(strategy_results["bearish"])
    if net_strategy_signal != 0:
        strategy_contribution = _clip(net_strategy_signal * 2, -12, 12)
        contribution += strategy_contribution
        fired = strategy_results["bullish"] if net_strategy_signal > 0 else strategy_results["bearish"]
        names = ", ".join(s.name for s in fired)
        reasons.append(
            f"Strategy confluence: {len(fired)} setup(s) agree "
            f"{'bullish' if net_strategy_signal > 0 else 'bearish'} ({names})"
        )

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


def classify_indicator_alignment(snapshot: dict) -> str | None:
    """Checks whether EMA structure, momentum (proxy for MACD), and RSI all agree
    on one direction ('all up' or 'all down'). Returns 'BULLISH', 'BEARISH', or
    None if the indicators conflict (spec: never force a signal when indicators disagree).
    This is a simple rule-based alignment check, not a magic score."""
    ema_bullish = snapshot["price_vs_ema20"] == "above" and snapshot["ema20_vs_ema50"] == "above"
    ema_bearish = snapshot["price_vs_ema20"] == "below" and snapshot["ema20_vs_ema50"] == "below"

    momentum_bullish = snapshot["change_pct"] > 0.3
    momentum_bearish = snapshot["change_pct"] < -0.3

    rsi = snapshot["rsi"]
    rsi_bullish = 50 <= rsi <= 75
    rsi_bearish = 25 <= rsi <= 50

    if ema_bullish and momentum_bullish and rsi_bullish:
        return "BULLISH"
    if ema_bearish and momentum_bearish and rsi_bearish:
        return "BEARISH"
    return None


# ---------------------------------------------------------------------------
# AI TRADE FINDER (spec sections: CE/PE independent analysis, strike selection,
# no-trade condition). CE and PE are scored independently - the system does not
# assume direction from one indicator alone, and does not force a trade when
# nothing clears the minimum bar.
# ---------------------------------------------------------------------------
MIN_TRADE_PROBABILITY = 60.0
MIN_RISK_REWARD = 1.5


def evaluate_strike_candidate(option: dict, option_type: str, direction_probability: float, spot: float) -> dict:
    """Scores one strike/option-type combination.

    OI-FIRST RULE: the option chain's real OI value added by calls vs. puts
    at this strike (already computed in mock_data.generate_mock_option_chain
    as `bias`/`final_signal`) is the PRIMARY driver of this score - not the
    underlying indicator/AI probability. A strike the OI data doesn't
    support for this option_type starts well below the qualifying bar and
    can only be nudged a little by indicator confidence, never flipped into
    a recommended trade purely on indicator strength. This mirrors the same
    rule already applied in main.py's /options/best-setup (candidates are
    pre-filtered by OI direction before probability is layered on)."""
    expected_signal = "CALL BUY" if option_type == "CE" else "PUT BUY"
    oi_bias = option.get("final_signal")
    reasons = []

    if oi_bias is not None:
        oi_supportive = (oi_bias == expected_signal)
        if oi_supportive:
            contribution = 62.0  # OI already favours this side - starts just above the qualifying bar
            reasons.append(f"Option-chain OI signal supports {option_type} here ({oi_bias})")
        else:
            contribution = 30.0  # OI does not favour this side - starts well below the bar
            reasons.append(f"Option-chain OI signal does NOT support {option_type} here (says {oi_bias} instead)")
        # Indicator/AI probability is applied AFTER the OI gate, as a smaller
        # confidence nudge only - even a 100% indicator reading can only add
        # +15, and can't lift an OI-unsupported strike (capped ~45) above the
        # 60% qualifying bar on its own.
        indicator_nudge = round((direction_probability - 50) * 0.3, 1)
        contribution += indicator_nudge
        reasons.append(f"Indicator/AI confidence nudge: {'+' if indicator_nudge >= 0 else ''}{indicator_nudge} (underlying {direction_probability}%)")
    else:
        # Fallback only - real chains always carry `final_signal` from
        # mock_data/live overlay; this path is just defensive.
        contribution = direction_probability
        reasons.append(f"Underlying {('bullish' if option_type == 'CE' else 'bearish')} probability: {direction_probability}%")

    # OI supportiveness (this side's own OI build - a secondary, finer-grained
    # signal on top of the bias gate above: even within an OI-supported
    # strike, stronger OI buildup nudges the score a bit further)
    if option["oi_change_pct"] > 15:
        contribution += 6
        reasons.append(f"{option_type} OI building up strongly (+{option['oi_change_pct']}%)")
    elif option["oi_change_pct"] > 0:
        contribution += 2
        reasons.append(f"{option_type} OI mildly supportive (+{option['oi_change_pct']}%)")
    else:
        contribution -= 4
        reasons.append(f"{option_type} OI not supportive ({option['oi_change_pct']}%)")

    # IV: too high makes the option expensive / risky
    if option["iv"] > 20:
        contribution -= 4
        reasons.append(f"IV relatively high ({option['iv']}) - option pricier")
    else:
        reasons.append(f"IV acceptable ({option['iv']})")

    # Liquidity via volume
    if option["volume"] < 5000:
        contribution -= 8
        reasons.append("Liquidity is thin for this strike")
    else:
        reasons.append("Liquidity is adequate")

    # Distance from spot (moneyness) - prefer near-ATM to slightly OTM, not deep OTM
    distance_pct = abs(option["strike"] - spot) / spot * 100
    if distance_pct > 3:
        contribution -= 6
        reasons.append(f"Strike is far from spot ({round(distance_pct, 1)}% away)")
    else:
        reasons.append(f"Strike is close to spot ({round(distance_pct, 1)}% away)")

    probability = round(_clip(contribution), 1)

    entry = option["ltp"]
    atr_like = max(entry * 0.06, 3)
    buy_low, buy_high = round(entry - atr_like * 0.3, 2), round(entry + atr_like * 0.3, 2)
    target_1 = round(entry + atr_like * 1.2, 2)
    target_2 = round(entry + atr_like * 2.2, 2)
    target_3 = round(entry + atr_like * 3.2, 2)
    stop_loss = round(max(entry - atr_like * 1.0, 0.5), 2)

    risk = entry - stop_loss
    reward = target_1 - entry
    risk_reward = round(reward / risk, 2) if risk > 0 else 0

    return {
        "option_type": option_type,
        "strike": option["strike"],
        "probability": probability,
        "entry": entry,
        "buy_range_low": buy_low,
        "buy_range_high": buy_high,
        "target_1": target_1,
        "target_2": target_2,
        "target_3": target_3,
        "stop_loss": stop_loss,
        "risk_reward": risk_reward,
        "liquidity_ok": option["volume"] >= 5000,
        "reasons": reasons,
    }


def find_best_trade(chain: dict, bullish_probability: float) -> dict:
    """Independently evaluates every CE and every PE in the chain, ranks all
    candidates together, and returns the best one only if it clears both the
    minimum probability and minimum risk/reward bar. Otherwise reports that no
    high-quality trade was found - the system never forces a recommendation."""
    bearish_probability = round(100 - bullish_probability, 1)
    spot = chain["spot"]

    candidates = []
    for opt in chain["calls"]:
        candidates.append(evaluate_strike_candidate(opt, "CE", bullish_probability, spot))
    for opt in chain["puts"]:
        candidates.append(evaluate_strike_candidate(opt, "PE", bearish_probability, spot))

    candidates.sort(key=lambda c: c["probability"], reverse=True)

    qualified = [
        c for c in candidates
        if c["probability"] >= MIN_TRADE_PROBABILITY
        and c["risk_reward"] >= MIN_RISK_REWARD
        and c["liquidity_ok"]
    ]

    top_5 = candidates[:5]

    if not qualified:
        return {
            "status": "NO_HIGH_QUALITY_TRADE_FOUND",
            "best_trade": None,
            "top_candidates": top_5,
            "reason": "No CE or PE candidate cleared the minimum probability "
                      f"({MIN_TRADE_PROBABILITY}%), minimum risk/reward ({MIN_RISK_REWARD}), "
                      "and liquidity bar at the same time.",
        }

    best = qualified[0]
    invalidation = [
        "Price breaks back below the entry structure (support/VWAP breakdown for CE, resistance/VWAP reclaim for PE)",
        "Probability drops below the entry threshold as new data comes in",
        f"Stop loss at \u20b9{best['stop_loss']} is hit",
    ]
    return {
        "status": "TRADE_FOUND",
        "best_trade": {**best, "invalidation": invalidation},
        "top_candidates": top_5,
        "reason": None,
    }
