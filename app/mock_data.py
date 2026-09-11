"""
MOCK DATA MODE (spec section 19).

Generates realistic-looking stock and option data WITHOUT any broker
API. This lets the whole app - dashboard, probability engine,
backtesting - be built and tested before DhanHQ credentials exist.

Later, replace the functions in this file with real calls to the
DhanHQ API. Nothing else in the app needs to change, because every
other module only depends on the shape of the data returned here
(same field names), not on where it came from.
"""

import random
from datetime import datetime

NIFTY_50_SAMPLE = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "AXISBANK",
    "LT", "SBIN", "BHARTIARTL", "KOTAKBANK", "ITC", "MARUTI",
    "SUNPHARMA", "WIPRO", "ONGC", "CIPLA", "TATAMOTORS", "HCLTECH",
]

# Sector tag for each stock above - used to group the "sector performance"
# strip and to show a SECTOR column in the buy/sell stock tables, matching
# the reference dashboard's layout. Any symbol not in this map (e.g. one
# typed into a search box) falls back to "OTHERS".
SECTOR_MAP = {
    "RELIANCE": "ENERGY", "ONGC": "ENERGY",
    "HDFCBANK": "PVT BANK", "ICICIBANK": "PVT BANK", "AXISBANK": "PVT BANK", "KOTAKBANK": "PVT BANK", "SBIN": "PSU BANK",
    "INFY": "IT", "TCS": "IT", "WIPRO": "IT", "HCLTECH": "IT",
    "LT": "CAP GOODS", "BHARTIARTL": "TELECOM", "ITC": "FMCG",
    "MARUTI": "AUTO", "TATAMOTORS": "AUTO",
    "SUNPHARMA": "PHARMA", "CIPLA": "PHARMA",
}
SECTOR_NAMES = sorted(set(SECTOR_MAP.values()) | {"METAL", "REALTY", "CHEMICALS", "IT SERVICE", "OIL AND GAS", "CONS DURBL", "FIN SERVICE", "DEFENCE", "MEDIA"})


def _seeded_random(symbol: str) -> random.Random:
    """Each symbol gets its own stable-ish random stream per process run,
    so numbers don't jump around wildly between two calls in the same second."""
    return random.Random(symbol)


def _with_change_abs(ltp: float, change_pct: float) -> dict:
    """Given an ltp and change_pct, derives the absolute point change too -
    e.g. ltp=23,650 change_pct=-0.61 -> change_abs=-144.05 - so the frontend
    can show 'ltp   -144.05 (-0.61%)' together, same as Angel One's own app."""
    close = ltp / (1 + change_pct / 100) if (1 + change_pct / 100) else ltp
    return {"ltp": ltp, "change_pct": change_pct, "change_abs": round(ltp - close, 2)}


def generate_sector_performance() -> list[dict]:
    """DEMO/mock sector-wise %change strip (the coloured bar row at the top
    of the reference 'Market Overview' page). Real version would aggregate
    live %change across all FNO stocks per sector."""
    rnd = _seeded_random("sector-perf")
    rows = [{"sector": s, "change_pct": round(rnd.uniform(-2.5, 1.0), 2)} for s in SECTOR_NAMES]
    rows.sort(key=lambda r: r["change_pct"], reverse=True)
    return rows


def generate_market_overview() -> dict:
    return {
        "NIFTY": _with_change_abs(round(24950.40 + random.uniform(-50, 50), 2), round(random.uniform(-1, 1.2), 2)),
        "BANK_NIFTY": _with_change_abs(round(53621.45 + random.uniform(-100, 100), 2), round(random.uniform(-1, 1.5), 2)),
        "SENSEX": _with_change_abs(round(81330.56 + random.uniform(-150, 150), 2), round(random.uniform(-1, 1), 2)),
        "INDIA_VIX": {"value": round(14.32 + random.uniform(-2, 2), 2), "change_pct": round(random.uniform(-3, 3), 2)},
        "ADX": {"value": round(random.uniform(14, 38), 1), "change_pct": round(random.uniform(-4, 4), 2)},
        "RSI": {"value": round(random.uniform(35, 70), 1), "change_pct": round(random.uniform(-3, 3), 2)},
        "market_regime": random.choice(["STRONG_BULLISH", "BULLISH", "SIDEWAYS", "BEARISH", "STRONG_BEARISH"]),
    }


def generate_stock_snapshot(symbol: str) -> dict:
    rnd = _seeded_random(symbol)
    base_price = round(rnd.uniform(200, 4000), 2)
    change_pct = round(rnd.uniform(-3, 3), 2)
    pdc = round(base_price / (1 + change_pct / 100), 2) if (1 + change_pct / 100) else base_price
    return {
        "symbol": symbol,
        "sector": SECTOR_MAP.get(symbol, "OTHERS"),
        "ltp": base_price,
        "change_pct": change_pct,
        "rsi": round(rnd.uniform(20, 80), 1),
        "volume": rnd.randint(500_000, 20_000_000),
        "price_vs_ema20": rnd.choice(["above", "below"]),
        "ema20_vs_ema50": rnd.choice(["above", "below"]),
        "price_vs_vwap": rnd.choice(["above", "below"]),
        "atr": round(rnd.uniform(5, 80), 2),
        "pdh": round(pdc * (1 + rnd.uniform(0.002, 0.02)), 2),
        "pdl": round(pdc * (1 - rnd.uniform(0.002, 0.02)), 2),
        # --- extra mocked indicator fields, added to drive strategies.py ---
        # (snapshot-only mock mode: these are independently randomised, same
        # as the fields above - they are NOT derived from a real candle
        # series. In live mode these should come from actual indicator
        # calculations off historical OHLC data.)
        "ema9_vs_ema21": rnd.choice(["above", "below"]),
        "ema50_vs_ema200": rnd.choice(["above", "below"]),
        "macd_cross": rnd.choice(["bullish", "bearish", "none"]),
        "adx": round(rnd.uniform(8, 45), 1),
        "bb_position": rnd.choice(["upper", "lower", "middle"]),
        "stoch_rsi_cross": rnd.choice(["bullish", "bearish", "none"]),
        "price_vs_parabolic_sar": rnd.choice(["above", "below"]),
        "price_vs_pivot": rnd.choice(["above_r1", "below_s1", "between"]),
    }


def generate_universe_snapshot(universe: list[str] = NIFTY_50_SAMPLE) -> list[dict]:
    return [generate_stock_snapshot(sym) for sym in universe]


def generate_timeframe_zones() -> dict:
    """Mock multi-timeframe market bias, similar to a 'Nifty Zone' panel:
    each timeframe independently mock-classified as bullish/bearish."""
    timeframes = ["5 MIN", "15 MIN", "30 MIN", "1 HR", "1 DAY", "1 WEEK"]
    rnd = _seeded_random("timeframe-zones")
    return {tf: rnd.choice(["BULLISH", "BEARISH"]) for tf in timeframes}


def generate_advance_decline() -> dict:
    rnd = _seeded_random("adv-decl")
    advances = rnd.randint(600, 1800)
    declines = rnd.randint(400, 1600)
    return {"advances": advances, "declines": declines}


def generate_nifty_zone(spot: float, underlying: str = "NIFTY") -> dict:
    """Pivot-point support/resistance 'zone' panel (mirrors the NIFTY ZONE
    box from the reference dashboard): PDC/open/day-range plus classic
    pivot S1-S4 / R1-R4 levels, and a highlighted 'current zone' band."""
    rnd = _seeded_random("nifty-zone-" + underlying)
    pdc = round(spot - rnd.uniform(-90, 90), 2)
    open_ = round(pdc + rnd.uniform(-45, 45), 2)
    low = round(min(open_, spot) - rnd.uniform(15, 60), 2)
    high = round(max(open_, spot) + rnd.uniform(15, 60), 2)
    low_30 = round(low - rnd.uniform(10, 40), 2)
    high_30 = round(high + rnd.uniform(10, 40), 2)
    avg = round((low + high) / 2, 2)

    pivot = round((high + low + pdc) / 3, 2)
    r1 = round(2 * pivot - low, 2)
    s1 = round(2 * pivot - high, 2)
    r2 = round(pivot + (high - low), 2)
    s2 = round(pivot - (high - low), 2)
    r3 = round(high + 2 * (pivot - low), 2)
    s3 = round(low - 2 * (high - pivot), 2)
    r4 = round(r3 + (r2 - r1), 2)
    s4 = round(s3 - (s1 - s2), 2)

    zone_low = round(min(s1, low_30), 2)
    zone_high = round(max(r1, high_30), 2)

    return {
        "index": underlying,
        "pdc": pdc, "open": open_, "low": low, "high": high,
        "low_30": low_30, "high_30": high_30, "avg": avg,
        "pivot_close": pdc,
        "supports": {"S1": s1, "S2": s2, "S3": s3, "S4": s4},
        "resistances": {"R1": r1, "R2": r2, "R3": r3, "R4": r4},
        "zone": {"low": zone_low, "high": zone_high},
    }


def generate_market_breadth() -> dict:
    """Advance/Decline/Unchanged breakdown for NIFTY 50 / NIFTY BANK / FNO,
    for both the pre-open session and the current live session."""
    def _breadth(seed: str) -> dict:
        rnd = _seeded_random(seed)
        return {
            "advances": rnd.randint(4, 100),
            "declines": rnd.randint(4, 130),
            "unchanged": rnd.randint(0, 40),
        }

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pre_open": {
            "NIFTY_50": _breadth("preopen-n50"),
            "NIFTY_BANK": _breadth("preopen-nbank"),
            "FNO": _breadth("preopen-fno"),
        },
        "live": {
            "NIFTY_50": _breadth("live-n50"),
            "NIFTY_BANK": _breadth("live-nbank"),
            "FNO": _breadth("live-fno"),
        },
    }


def generate_mock_backtest_summary() -> dict:
    """Generates a plausible-looking calibration report (spec section 10) for
    demo purposes, since no real trading history exists yet in mock mode.
    Clearly a simulation - not derived from actual logged trades."""
    rnd = _seeded_random("backtest-demo")
    buckets = {}
    total_trades = 0
    total_wins = 0
    for low in range(50, 90, 5):
        bucket_trades = rnd.randint(15, 60)
        # win rate roughly tracks the predicted bucket, with some noise,
        # to look like a reasonably-calibrated (but still fake) model
        target_rate = (low + 2.5) / 100
        noise = rnd.uniform(-0.05, 0.05)
        wins = round(bucket_trades * max(0.3, min(0.95, target_rate + noise)))
        buckets[f"{low}-{low+5}%"] = {
            "total_trades": bucket_trades,
            "wins": wins,
            "actual_win_rate": round(wins / bucket_trades * 100, 1) if bucket_trades else None,
        }
        total_trades += bucket_trades
        total_wins += wins

    return {
        "is_demo_data": True,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "overall_win_rate": round(total_wins / total_trades * 100, 1) if total_trades else 0,
        "buckets": buckets,
    }


_NEWS_TEMPLATES = {
    "POSITIVE": [
        "{sym} gains on strong sector demand and positive brokerage commentary.",
        "Analysts raise target price on {sym} citing improved quarterly outlook.",
        "{sym} sees heavy institutional buying interest in early trade.",
        "Positive global cues lift {sym} along with broader sector peers.",
    ],
    "NEGATIVE": [
        "{sym} slips on profit booking after recent rally.",
        "Weak global cues weigh on {sym} and sector peers.",
        "Analysts flag margin pressure concerns for {sym} this quarter.",
        "{sym} sees FII selling pressure amid broader market caution.",
    ],
    "NEUTRAL": [
        "{sym} trades range-bound ahead of upcoming earnings.",
        "No major triggers for {sym} today; market awaits macro cues.",
        "{sym} consolidates near recent levels in a quiet session.",
    ],
}


def generate_mock_news(symbol: str, count: int = 3) -> list[dict]:
    """DEMO/mock news+sentiment headlines for `symbol` - there is no real
    news feed wired in yet. Swap this out for a real news/sentiment API
    (e.g. a news aggregator or broker news feed) later; everything else
    (ai_engine.py) only depends on this list-of-dicts shape, not on where
    the headlines came from."""
    rnd = _seeded_random("news-" + symbol)
    sentiments = rnd.choices(["POSITIVE", "NEGATIVE", "NEUTRAL"], k=count)
    return [
        {
            "headline": rnd.choice(_NEWS_TEMPLATES[s]).format(sym=symbol),
            "sentiment": s,
            "is_mock": True,
        }
        for s in sentiments
    ]


def format_indian_value(n: float) -> str:
    """Formats a raw contract count into Indian Cr/L shorthand, e.g. 15_500_000 -> '1.55Cr'."""
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_00_00_000:
        return f"{sign}{n / 1_00_00_000:.2f}Cr"
    if n >= 1_00_000:
        return f"{sign}{n / 1_00_000:.2f}L"
    return f"{sign}{n:.0f}"


def generate_mock_option_chain(underlying: str, spot: float, expiry: str) -> dict:
    """Builds a mock option chain around the spot price with strikes at round intervals.
    Includes Greeks, bid/ask, change fields, and OI-buildup-based bias per strike
    (spec section 5). The BIAS / FINAL SIGNAL fields are derived from which side
    (calls or puts) is adding the bigger real OI value at that strike - not from
    the stock/indicator probability engine. Indicator + AI scoring (probability.py)
    is only applied afterwards, on top of strikes the OI data already favours -
    see /options/best-setup, which now ranks by OI buildup first."""
    step = 100 if underlying in ("NIFTY", "BANKNIFTY", "SENSEX") else 50
    base_strike = round(spot / step) * step
    strikes = [base_strike + (i * step) for i in range(-4, 5)]

    rnd = _seeded_random(underlying + expiry)
    calls, puts = [], []
    for strike in strikes:
        call_ltp = round(max(1, (spot - strike) * 0.4 + rnd.uniform(20, 60)), 2)
        put_ltp = round(max(1, (strike - spot) * 0.4 + rnd.uniform(20, 60)), 2)
        moneyness = abs(strike - spot) / spot
        # OI concentrates near the money, like a real chain - strikes far from
        # spot carry much smaller open interest than ATM/near-ATM strikes.
        oi_scale = max(0.05, 1 - min(moneyness * 6, 0.95))

        call_change_pct = round(rnd.uniform(-8, 12), 2)
        put_change_pct = round(rnd.uniform(-8, 12), 2)
        call_oi = rnd.randint(200_000, 25_000_000)
        call_oi = round(call_oi * oi_scale) + rnd.randint(50_000, 300_000)
        put_oi = rnd.randint(200_000, 25_000_000)
        put_oi = round(put_oi * oi_scale) + rnd.randint(50_000, 300_000)
        call_oi_chg_pct = round(rnd.uniform(-20, 45), 1)
        put_oi_chg_pct = round(rnd.uniform(-20, 45), 1)

        calls.append({
            "strike": strike,
            "ltp": call_ltp,
            "change_pct": call_change_pct,
            "oi": call_oi,
            "oi_fmt": format_indian_value(call_oi),
            "oi_change_pct": call_oi_chg_pct,
            "oi_change_value": round(call_oi * call_oi_chg_pct / 100),
            "oi_change_fmt": format_indian_value(call_oi * call_oi_chg_pct / 100),
            "iv": round(rnd.uniform(11, 22), 1),
            "delta": round(max(0.02, min(0.98, 0.5 - (strike - spot) / spot * 3)), 2),
            "gamma": round(max(0.0005, 0.01 * (1 - min(moneyness * 8, 0.95))), 4),
            "theta": round(-rnd.uniform(2, 12), 2),
            "vega": round(rnd.uniform(3, 18) * (1 - min(moneyness * 5, 0.8)), 2),
            "bid": round(call_ltp - rnd.uniform(0.3, 1.5), 2),
            "ask": round(call_ltp + rnd.uniform(0.3, 1.5), 2),
            "volume": rnd.randint(1000, 100_000),
            "greeks_source": "mock",
            "option_source": "mock",
        })
        puts.append({
            "strike": strike,
            "ltp": put_ltp,
            "change_pct": put_change_pct,
            "oi": put_oi,
            "oi_fmt": format_indian_value(put_oi),
            "oi_change_pct": put_oi_chg_pct,
            "oi_change_value": round(put_oi * put_oi_chg_pct / 100),
            "oi_change_fmt": format_indian_value(put_oi * put_oi_chg_pct / 100),
            "iv": round(rnd.uniform(11, 22), 1),
            "delta": round(-max(0.02, min(0.98, 0.5 + (strike - spot) / spot * 3)), 2),
            "gamma": round(max(0.0005, 0.01 * (1 - min(moneyness * 8, 0.95))), 4),
            "theta": round(-rnd.uniform(2, 12), 2),
            "vega": round(rnd.uniform(3, 18) * (1 - min(moneyness * 5, 0.8)), 2),
            "bid": round(put_ltp - rnd.uniform(0.3, 1.5), 2),
            "ask": round(put_ltp + rnd.uniform(0.3, 1.5), 2),
            "volume": rnd.randint(1000, 100_000),
            "greeks_source": "mock",
            "option_source": "mock",
        })

    # --- OI-buildup multiplier vs the chain's own average add, per side ---
    call_adds = [abs(c["oi_change_value"]) for c in calls]
    put_adds = [abs(p["oi_change_value"]) for p in puts]
    avg_call_add = (sum(call_adds) / len(call_adds)) or 1
    avg_put_add = (sum(put_adds) / len(put_adds)) or 1
    for c in calls:
        c["oi_change_multiplier"] = round(abs(c["oi_change_value"]) / avg_call_add, 1)
    for p in puts:
        p["oi_change_multiplier"] = round(abs(p["oi_change_value"]) / avg_put_add, 1)

    # --- Per-strike BIAS from real OI value added, not from indicators ---
    # Call OI building up faster than Put OI at a strike = writers defending
    # that level as resistance -> bearish (PUT BUY). The reverse = support
    # forming -> bullish (CALL BUY). A close contest = MIXED OI.
    total_call_add = sum(c["oi_change_value"] for c in calls)
    total_put_add = sum(p["oi_change_value"] for p in puts)
    overall_bias = "BEARISH" if total_call_add >= total_put_add else "BULLISH"
    overall_final_signal = "PUT BUY" if overall_bias == "BEARISH" else "CALL BUY"

    for c, p in zip(calls, puts):
        call_add, put_add = c["oi_change_value"], p["oi_change_value"]
        bigger = max(abs(call_add), abs(put_add)) or 1
        gap_ratio = abs(call_add - put_add) / bigger
        if gap_ratio < 0.15:
            bias = "MIXED OI"
            final_signal = overall_final_signal
        elif call_add > put_add:
            bias = "PUT BUY"
            final_signal = "PUT BUY"
        else:
            bias = "CALL BUY"
            final_signal = "CALL BUY"
        c["bias"] = bias
        c["final_signal"] = final_signal
        p["bias"] = bias
        p["final_signal"] = final_signal

    return {
        "underlying": underlying, "spot": spot, "expiry": expiry, "calls": calls, "puts": puts,
        "overall_oi_bias": overall_bias,
        "overall_final_signal": overall_final_signal,
        "timeframe_oi_bias": generate_oi_bias_timeframes(overall_bias),
    }


def generate_oi_bias_timeframes(overall_bias: str) -> dict:
    """Mock multi-timeframe OI bias strip (OI / LATEST / 3M / 5M / 15M / 30M),
    mostly agreeing with the chain-wide OI bias with a little noise per timeframe -
    mirrors the reference dashboard's bias row."""
    rnd = _seeded_random("oi-bias-tf")
    other = "BULLISH" if overall_bias == "BEARISH" else "BEARISH"
    labels = ["OI", "LATEST", "3M", "5M", "15M", "30M"]
    return {lbl: (overall_bias if rnd.random() > 0.15 else other) for lbl in labels}
