"""
Strategy Rule Engine.

Translates the named technical-analysis setups (EMA crossovers, RSI+EMA,
MACD+Bollinger, VWAP+EMA, Parabolic SAR+Stochastic RSI, RSI+ADX, pivot
points, etc.) into small, independent, honestly-gated rule functions.

Design rules (same philosophy as probability.py and
classify_indicator_alignment):
  1. Each strategy only returns "BULLISH" / "BEARISH" when ALL of its own
     named conditions line up - never a partial/forced signal.
  2. If conditions don't line up, it returns None ("no setup here right
     now"), not a guess.
  3. Every fired signal carries a plain-language reason, same as the rest
     of the codebase, for the AI-explanation layer (spec section 15).
  4. Nothing here invents a probability number on its own - strategies
     only ever return a direction + reason. Turning "how many strategies
     agree" into a probability number is done by score_stock() in
     probability.py, the same place all other scoring happens.

IMPORTANT LIMITATION (mock-data mode): mock_data.generate_stock_snapshot
returns a single random snapshot per symbol, not a real OHLC candle
series. That means setups which are only meaningful across MULTIPLE bars
- candlestick patterns (Morning/Evening Star, Three White Soldiers...),
chart patterns (wedges, double bottom, trendline breaks), SMC concepts
(liquidity sweeps, order blocks, FVG), and true Fibonacci retracement
(which needs a real swing high/low) - are NOT implemented here, because
faking them from a single snapshot would just be a random number with a
technical-sounding label attached to it. Those need real historical
price data. They're listed at the bottom of this file as NOT_IMPLEMENTED
so it's clear what's covered and what still needs live data.
"""

from dataclasses import dataclass


@dataclass
class StrategySignal:
    name: str
    signal: str  # "BULLISH" or "BEARISH"
    reason: str


def ema_9_21_strategy(snapshot: dict) -> StrategySignal | None:
    """9 EMA + 21 EMA crossover strategy (trend-following)."""
    if snapshot["ema9_vs_ema21"] == "above" and snapshot["price_vs_ema20"] == "above":
        return StrategySignal("EMA 9/21", "BULLISH", "9 EMA above 21 EMA, price above the EMAs")
    if snapshot["ema9_vs_ema21"] == "below" and snapshot["price_vs_ema20"] == "below":
        return StrategySignal("EMA 9/21", "BEARISH", "9 EMA below 21 EMA, price below the EMAs")
    return None


def ema_20_50_crossover_strategy(snapshot: dict) -> StrategySignal | None:
    """20 EMA / 50 EMA crossover strategy (ride-the-trend)."""
    if snapshot["ema20_vs_ema50"] == "above" and snapshot["price_vs_ema20"] == "above":
        return StrategySignal("EMA 20/50", "BULLISH", "20 EMA above 50 EMA in an uptrend")
    if snapshot["ema20_vs_ema50"] == "below" and snapshot["price_vs_ema20"] == "below":
        return StrategySignal("EMA 20/50", "BEARISH", "20 EMA below 50 EMA in a downtrend")
    return None


def ema_50_200_strategy(snapshot: dict) -> StrategySignal | None:
    """50 EMA / 200 EMA strategy (golden/death cross, long-term trend)."""
    if snapshot["ema50_vs_ema200"] == "above" and snapshot["ema20_vs_ema50"] == "above":
        return StrategySignal("EMA 50/200", "BULLISH", "50 EMA above 200 EMA (golden-cross side), short-term trend agrees")
    if snapshot["ema50_vs_ema200"] == "below" and snapshot["ema20_vs_ema50"] == "below":
        return StrategySignal("EMA 50/200", "BEARISH", "50 EMA below 200 EMA (death-cross side), short-term trend agrees")
    return None


def rsi_ema_20_50_strategy(snapshot: dict) -> StrategySignal | None:
    """RSI + EMA 20 & 50 strategy: 20 EMA vs 50 EMA sets the trend, RSI(14)
    crossing the 50 line confirms momentum in that same direction."""
    rsi = snapshot["rsi"]
    if snapshot["ema20_vs_ema50"] == "above" and snapshot["price_vs_ema20"] == "above" and rsi > 50:
        return StrategySignal("RSI+EMA 20&50", "BULLISH", f"20 EMA > 50 EMA, price above 20 EMA, RSI above 50 ({rsi})")
    if snapshot["ema20_vs_ema50"] == "below" and snapshot["price_vs_ema20"] == "below" and rsi < 50:
        return StrategySignal("RSI+EMA 20&50", "BEARISH", f"20 EMA < 50 EMA, price below 20 EMA, RSI below 50 ({rsi})")
    return None


def vwap_ema_strategy(snapshot: dict) -> StrategySignal | None:
    """VWAP + EMA strategy: enter long only when price has closed above
    BOTH VWAP and the EMA; short only when below both."""
    if snapshot["price_vs_vwap"] == "above" and snapshot["price_vs_ema20"] == "above":
        return StrategySignal("VWAP+EMA", "BULLISH", "Price closed above VWAP and above the EMA")
    if snapshot["price_vs_vwap"] == "below" and snapshot["price_vs_ema20"] == "below":
        return StrategySignal("VWAP+EMA", "BEARISH", "Price closed below VWAP and below the EMA")
    return None


def macd_bollinger_strategy(snapshot: dict) -> StrategySignal | None:
    """MACD + Bollinger Bands strategy: lower-band touch + bullish MACD
    crossover = buy; upper-band touch + bearish MACD crossover = sell."""
    if snapshot["bb_position"] == "lower" and snapshot["macd_cross"] == "bullish":
        return StrategySignal("MACD+BB", "BULLISH", "Price at lower Bollinger Band with a bullish MACD crossover")
    if snapshot["bb_position"] == "upper" and snapshot["macd_cross"] == "bearish":
        return StrategySignal("MACD+BB", "BEARISH", "Price at upper Bollinger Band with a bearish MACD crossover")
    return None


def parabolic_sar_stoch_rsi_strategy(snapshot: dict) -> StrategySignal | None:
    """Parabolic SAR + Stochastic RSI strategy (long only above SAR with a
    bullish %K/%D cross; short only below SAR with a bearish cross)."""
    if snapshot["price_vs_parabolic_sar"] == "above" and snapshot["stoch_rsi_cross"] == "bullish":
        return StrategySignal("PSAR+StochRSI", "BULLISH", "Price above Parabolic SAR, Stochastic RSI %K crossed above %D")
    if snapshot["price_vs_parabolic_sar"] == "below" and snapshot["stoch_rsi_cross"] == "bearish":
        return StrategySignal("PSAR+StochRSI", "BEARISH", "Price below Parabolic SAR, Stochastic RSI %K crossed below %D")
    return None


def rsi_adx_strategy(snapshot: dict) -> StrategySignal | None:
    """RSI + ADX strategy. Per the reference setup this is a fading/short
    setup: RSI below 70 (not overbought) + ADX crossing below 25 (trend
    losing strength) favours a short. Mirrored for the long side: RSI
    above 30 (not oversold) + ADX below 25 favours covering shorts/going
    long into the weak trend."""
    rsi = snapshot["rsi"]
    adx = snapshot["adx"]
    if rsi < 70 and adx < 25 and snapshot["change_pct"] < 0:
        return StrategySignal("RSI+ADX", "BEARISH", f"RSI below 70 ({rsi}), ADX below 25 ({adx}) - weakening trend, short bias")
    if rsi > 30 and adx < 25 and snapshot["change_pct"] > 0:
        return StrategySignal("RSI+ADX", "BULLISH", f"RSI above 30 ({rsi}), ADX below 25 ({adx}) - weakening trend, long bias")
    return None


def pivot_point_strategy(snapshot: dict) -> StrategySignal | None:
    """Pivot point strategy: close above R1 favours longs (R2/R3 as
    targets); close below S1 favours shorts (S2/S3 as targets)."""
    if snapshot["price_vs_pivot"] == "above_r1":
        return StrategySignal("Pivot Points", "BULLISH", "Price closed above the R1 pivot level")
    if snapshot["price_vs_pivot"] == "below_s1":
        return StrategySignal("Pivot Points", "BEARISH", "Price closed below the S1 pivot level")
    return None


# All strategies that can run off a single mock snapshot. Add new ones here
# as more mocked indicator fields are introduced.
ALL_STRATEGIES = [
    ema_9_21_strategy,
    ema_20_50_crossover_strategy,
    ema_50_200_strategy,
    rsi_ema_20_50_strategy,
    vwap_ema_strategy,
    macd_bollinger_strategy,
    parabolic_sar_stoch_rsi_strategy,
    rsi_adx_strategy,
    pivot_point_strategy,
]

# Setups from the reference material that need a real multi-bar OHLC
# history (not just one snapshot) and are intentionally NOT implemented
# here yet - listed so it's clear what's covered vs. still pending live
# data:
#   - Candlestick patterns: Morning Star, Evening Star, Three White
#     Soldiers, Three Black Crows
#   - Chart patterns: Rising/Falling Wedge, Double Bottom, Trendline
#     Reversal & Break, Support/Resistance retest confirmation
#   - SMC concepts: Liquidity Sweep, Order Block, FVG, Market Structure
#     Shift/Break of Structure, Change of Character
#   - Fibonacci retracement (needs a real swing high/low, not a proxy)
#   - RSI trendline breakout (needs the RSI's own historical line)
NOT_IMPLEMENTED_NEEDS_CANDLE_HISTORY = [
    "morning_star", "evening_star", "three_white_soldiers", "three_black_crows",
    "rising_wedge", "falling_wedge", "double_bottom", "trendline_break",
    "support_resistance_retest", "smc_liquidity_sweep", "smc_order_block",
    "smc_fvg", "smc_market_structure_shift", "fibonacci_retracement",
    "rsi_trendline_breakout",
]


def run_all_strategies(snapshot: dict) -> dict:
    """Runs every implemented strategy against one snapshot and buckets
    the results. Returns which strategies fired bullish, which fired
    bearish, and how many stayed silent (no setup) - a strategy staying
    silent is a normal, expected outcome, not a failure."""
    bullish: list[StrategySignal] = []
    bearish: list[StrategySignal] = []
    silent: list[str] = []

    for strategy_fn in ALL_STRATEGIES:
        result = strategy_fn(snapshot)
        if result is None:
            silent.append(strategy_fn.__name__)
        elif result.signal == "BULLISH":
            bullish.append(result)
        else:
            bearish.append(result)

    return {
        "bullish": bullish,
        "bearish": bearish,
        "silent_count": len(silent),
        "total_count": len(ALL_STRATEGIES),
    }
def oi_cluster_support_resistance_strategy(chain: dict) -> StrategySignal | None:
    """OI Cluster + Support/Resistance + Market Reaction strategy.

    Works off the option chain's own calls/puts lists (mock_data.generate_mock_option_chain):
    each call/put dict already has strike, oi, oi_change_pct. Needs at least
    3 strikes on each side to form a cluster around ATM."""
    calls = chain.get("calls") or []
    puts = chain.get("puts") or []
    if len(calls) < 3 or len(puts) < 3:
        return None

    spot = chain["spot"]

    # Step 1: Support = strike with highest Put OI, Resistance = strike with highest Call OI
    support_row = max(puts, key=lambda p: p["oi"])
    resistance_row = max(calls, key=lambda c: c["oi"])
    support = support_row["strike"]
    resistance = resistance_row["strike"]
    if support >= resistance:
        return None  # unclear structure, don't guess

    put_oi_change_pct = support_row["oi_change_pct"]
    call_oi_change_pct = resistance_row["oi_change_pct"]

    support_strengthening = put_oi_change_pct > 5
    support_weakening = put_oi_change_pct < -5
    resistance_strengthening = call_oi_change_pct > 5
    resistance_weakening = call_oi_change_pct < -5

    near_support = spot <= support * 1.005
    near_resistance = spot >= resistance * 0.995
    broke_resistance = spot > resistance
    broke_support = spot < support

    reasons = [f"Support {support} (highest Put OI), Resistance {resistance} (highest Call OI)"]

    if support_strengthening and near_support and not broke_support:
        reasons.append(f"Put OI building at support ({put_oi_change_pct:+.1f}%), support holding")
        if resistance_weakening:
            reasons.append(f"Call OI unwinding at resistance ({call_oi_change_pct:+.1f}%) - resistance weak")
        return StrategySignal("OI Cluster S/R", "BULLISH", "; ".join(reasons))

    if broke_resistance and resistance_weakening:
        reasons.append(f"Call OI unwinding ({call_oi_change_pct:+.1f}%), price broke resistance {resistance}")
        return StrategySignal("OI Cluster S/R", "BULLISH", "; ".join(reasons))

    if resistance_strengthening and near_resistance and not broke_resistance:
        reasons.append(f"Call OI building at resistance ({call_oi_change_pct:+.1f}%), resistance holding")
        if support_weakening:
            reasons.append(f"Put OI unwinding at support ({put_oi_change_pct:+.1f}%) - support weak")
        return StrategySignal("OI Cluster S/R", "BEARISH", "; ".join(reasons))

    if broke_support and support_weakening:
        reasons.append(f"Put OI unwinding ({put_oi_change_pct:+.1f}%), price broke support {support}")
        return StrategySignal("OI Cluster S/R", "BEARISH", "; ".join(reasons))

    return None  # NO TRADE condition - unclear/contradictory, don't force a signal
