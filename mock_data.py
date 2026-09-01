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

NIFTY_50_SAMPLE = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "AXISBANK",
    "LT", "SBIN", "BHARTIARTL", "KOTAKBANK", "ITC", "MARUTI",
    "SUNPHARMA", "WIPRO", "ONGC", "CIPLA", "TATAMOTORS", "HCLTECH",
]


def _seeded_random(symbol: str) -> random.Random:
    """Each symbol gets its own stable-ish random stream per process run,
    so numbers don't jump around wildly between two calls in the same second."""
    return random.Random(symbol)


def generate_market_overview() -> dict:
    return {
        "NIFTY": {"ltp": round(24950.40 + random.uniform(-50, 50), 2), "change_pct": round(random.uniform(-1, 1.2), 2)},
        "BANK_NIFTY": {"ltp": round(53621.45 + random.uniform(-100, 100), 2), "change_pct": round(random.uniform(-1, 1.5), 2)},
        "SENSEX": {"ltp": round(81330.56 + random.uniform(-150, 150), 2), "change_pct": round(random.uniform(-1, 1), 2)},
        "INDIA_VIX": {"value": round(14.32 + random.uniform(-2, 2), 2), "change_pct": round(random.uniform(-3, 3), 2)},
        "market_regime": random.choice(["STRONG_BULLISH", "BULLISH", "SIDEWAYS", "BEARISH", "STRONG_BEARISH"]),
    }


def generate_stock_snapshot(symbol: str) -> dict:
    rnd = _seeded_random(symbol + str(random.random()))
    base_price = round(rnd.uniform(200, 4000), 2)
    return {
        "symbol": symbol,
        "ltp": base_price,
        "change_pct": round(rnd.uniform(-3, 3), 2),
        "rsi": round(rnd.uniform(20, 80), 1),
        "volume": rnd.randint(500_000, 20_000_000),
        "price_vs_ema20": rnd.choice(["above", "below"]),
        "ema20_vs_ema50": rnd.choice(["above", "below"]),
        "price_vs_vwap": rnd.choice(["above", "below"]),
        "atr": round(rnd.uniform(5, 80), 2),
    }


def generate_universe_snapshot(universe: list[str] = NIFTY_50_SAMPLE) -> list[dict]:
    return [generate_stock_snapshot(sym) for sym in universe]


def generate_mock_option_chain(underlying: str, spot: float, expiry: str) -> dict:
    """Builds a mock option chain around the spot price with strikes at round intervals."""
    step = 100 if underlying == "NIFTY" else (100 if underlying == "BANKNIFTY" else 50)
    base_strike = round(spot / step) * step
    strikes = [base_strike + (i * step) for i in range(-2, 3)]

    rnd = _seeded_random(underlying + expiry)
    calls, puts = [], []
    for strike in strikes:
        distance = strike - spot
        calls.append({
            "strike": strike,
            "ltp": round(max(1, (spot - strike) * 0.4 + rnd.uniform(20, 60)), 2),
            "oi": rnd.randint(10_000, 500_000),
            "oi_change_pct": round(rnd.uniform(-20, 40), 1),
            "iv": round(rnd.uniform(11, 22), 1),
            "volume": rnd.randint(1000, 100_000),
        })
        puts.append({
            "strike": strike,
            "ltp": round(max(1, (strike - spot) * 0.4 + rnd.uniform(20, 60)), 2),
            "oi": rnd.randint(10_000, 500_000),
            "oi_change_pct": round(rnd.uniform(-20, 40), 1),
            "iv": round(rnd.uniform(11, 22), 1),
            "volume": rnd.randint(1000, 100_000),
        })

    return {"underlying": underlying, "spot": spot, "expiry": expiry, "calls": calls, "puts": puts}
