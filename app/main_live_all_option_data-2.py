from datetime import datetime
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from . import models, schemas, auth, mock_data, probability, angel_one_client, ai_engine, oi_history
from .database import engine, get_db

models.Base.metadata.create_all(bind=engine)

logger = logging.getLogger("angel_one")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="AI Stock & Option Probability Analyzer", version="0.1.0-mock")

# Allow the frontend (running on a different domain, e.g. Vercel/Netlify)
# to call this API. Tighten allow_origins to your real frontend URL
# before going live.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DISCLAIMER = (
    "Probability is an analytical estimate based on market data and historical "
    "patterns. It does not guarantee profit. Trading and options trading involve "
    "substantial risk."
)

# Flip this on (env var USE_LIVE_MARKET_DATA=true) once Angel One credentials
# are set. Any live-data failure (bad creds, network, scrip master miss, etc.)
# is caught and silently falls back to mock_data - a broken broker connection
# should never take the whole dashboard down.
USE_LIVE_MARKET_DATA = os.environ.get("USE_LIVE_MARKET_DATA", "false").lower() == "true"
logger.info("USE_LIVE_MARKET_DATA=%s angel_one_configured=%s", USE_LIVE_MARKET_DATA, angel_one_client.is_configured())


def _live_overlay(data: dict, targets: list) -> dict:
    """Generic version: best-effort replace ltp+change_pct for each (angel_name,
    data_key) pair in `targets` with real Angel One prices, fetched in
    parallel (not one-after-another) since each is a real network round-trip -
    doing them sequentially was the main cause of the app feeling slow/stuck
    once live mode was on."""
    if not USE_LIVE_MARKET_DATA:
        return data
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        futures = {pool.submit(angel_one_client.get_index_ltp, underlying): (underlying, key) for underlying, key in targets}
        for future in as_completed(futures):
            underlying, key = futures[future]
            try:
                live = future.result()
                data[key]["ltp"] = live["ltp"]
                data[key]["change_pct"] = live["change_pct"]
                data[key]["change_abs"] = live.get("change_abs")
                data[key]["source"] = "live"
                logger.info("Live LTP OK for %s: %s", underlying, live)
            except Exception as exc:
                data[key]["source"] = "mock"
                data[key]["live_error"] = str(exc)
                logger.warning("Live LTP FAILED for %s: %s", underlying, exc)
    return data


def _live_index_overlay(overview: dict) -> dict:
    """ADX/RSI stay mock (Angel One doesn't give those directly - a later step)."""
    return _live_overlay(overview, [("NIFTY", "NIFTY"), ("BANKNIFTY", "BANK_NIFTY"), ("SENSEX", "SENSEX")])


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------
@app.post("/auth/register", response_model=schemas.Token)
def register(payload: schemas.UserCreate, db: Session = Depends(get_db)):
    existing = db.query(models.User).filter(models.User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = models.User(email=payload.email, hashed_password=auth.hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)

    token = auth.create_access_token({"sub": user.email})
    return schemas.Token(access_token=token)


@app.post("/auth/login", response_model=schemas.Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == form_data.username).first()
    if not user or not auth.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    token = auth.create_access_token({"sub": user.email})
    return schemas.Token(access_token=token)


@app.get("/auth/me")
def me(current_user: models.User = Depends(auth.get_current_user)):
    return {"email": current_user.email, "member_since": current_user.created_at}


# ---------------------------------------------------------------------------
# DASHBOARD (mock data mode - spec sections 2-4)
# ---------------------------------------------------------------------------
@app.get("/dashboard/overview")
def market_overview(current_user: models.User = Depends(auth.get_current_user)):
    overview = mock_data.generate_market_overview()
    overview = _live_index_overlay(overview)
    return {**overview, "disclaimer": DISCLAIMER, "live_data": USE_LIVE_MARKET_DATA}


@app.get("/dashboard/top-bullish")
def top_bullish(current_user: models.User = Depends(auth.get_current_user)):
    snapshots = mock_data.generate_universe_snapshot()
    scored = []
    for s in snapshots:
        result = probability.score_stock(s)
        scored.append({**s, "probability": result.probability, "reasons": result.reasons})
    scored.sort(key=lambda x: x["probability"], reverse=True)
    return {"stocks": scored[:10], "disclaimer": DISCLAIMER}


@app.get("/dashboard/top-bearish")
def top_bearish(current_user: models.User = Depends(auth.get_current_user)):
    snapshots = mock_data.generate_universe_snapshot()
    scored = []
    for s in snapshots:
        result = probability.score_stock(s)
        bearish_probability = round(100 - result.probability, 1)
        scored.append({**s, "probability": bearish_probability, "reasons": result.reasons})
    scored.sort(key=lambda x: x["probability"], reverse=True)
    return {"stocks": scored[:10], "disclaimer": DISCLAIMER}


@app.get("/stocks/lookup")
def stock_lookup(symbol: str, current_user: models.User = Depends(auth.get_current_user)):
    """Looks up ANY stock symbol on demand (not just the fixed 18-symbol
    sample used by top-bullish/top-bearish) - powers the search box on the
    Heatmap and AI Predictions / Option Analyzer pages. Returns the same
    snapshot + probability shape as those lists so the frontend can reuse
    the same row/tile renderers."""
    symbol = symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    snapshot = mock_data.generate_stock_snapshot(symbol)
    if USE_LIVE_MARKET_DATA:
        try:
            live = angel_one_client.get_stock_ltp(symbol)
            snapshot["ltp"] = live["ltp"]
            snapshot["change_pct"] = live["change_pct"]
            snapshot["source"] = "live"
        except Exception as exc:
            snapshot["source"] = "mock"
            snapshot["live_error"] = str(exc)
            logger.warning("Live LTP FAILED for searched stock %s: %s", symbol, exc)
    result = probability.score_stock(snapshot)
    return {**snapshot, "probability": result.probability, "reasons": result.reasons, "disclaimer": DISCLAIMER}


def _spot_for(underlying: str, overview: dict) -> float:
    """Maps an underlying symbol to its LTP from the market overview.
    (Previously this only branched NIFTY vs BANK_NIFTY, so SENSEX silently
    fell through to the BANK_NIFTY price - fixed here.)"""
    if underlying == "BANKNIFTY":
        return overview["BANK_NIFTY"]["ltp"]
    if underlying == "SENSEX":
        return overview["SENSEX"]["ltp"]
    return overview["NIFTY"]["ltp"]


_INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "SENSEX"}


def _resolve_spot(underlying: str) -> float:
    """Single entry point option-chain-family endpoints use to get a spot
    price, whether `underlying` is one of the 3 major indices or an
    individual F&O stock symbol (e.g. 'RELIANCE'). Stocks go through
    mock_data's per-symbol snapshot (mock) or angel_one_client.get_stock_ltp
    (live) instead of the index-only market overview."""
    if underlying in _INDEX_UNDERLYINGS:
        overview = mock_data.generate_market_overview()
        overview = _live_index_overlay(overview)
        return _spot_for(underlying, overview)
    snapshot = mock_data.generate_stock_snapshot(underlying)
    spot = snapshot["ltp"]
    if USE_LIVE_MARKET_DATA:
        try:
            live = angel_one_client.get_stock_ltp(underlying)
            spot = live["ltp"]
            logger.info("Live LTP OK for stock %s: %s", underlying, live)
        except Exception as exc:
            logger.warning("Live LTP FAILED for stock %s: %s", underlying, exc)
    return spot


@app.get("/fno/stocks")
def fno_stocks(current_user: models.User = Depends(auth.get_current_user)):
    """The stock universe the Option Chain / AI Trade Finder pages offer
    under a 'Stock F&O' segment, alongside the 3 index underlyings."""
    return {"stocks": mock_data.NIFTY_50_SAMPLE, "disclaimer": DISCLAIMER}


def _real_nifty_zone(spot: float, underlying: str) -> dict:
    """Classic floor-trader pivot (Pivot/R1-R4/S1-S4) computed from REAL
    previous-day High/Low/Close via Angel One's historical candle API -
    same formula mock_data.generate_nifty_zone uses, but fed real PDH/PDL/PDC
    instead of randomised ones, so the levels are actually meaningful for
    trading and stay fixed for the whole day (only the live spot moves)."""
    ohlc = angel_one_client.get_previous_day_ohlc(underlying)
    pdh, pdl, pdc = ohlc["pdh"], ohlc["pdl"], ohlc["pdc"]

    pivot = round((pdh + pdl + pdc) / 3, 2)
    r1 = round(2 * pivot - pdl, 2)
    s1 = round(2 * pivot - pdh, 2)
    r2 = round(pivot + (pdh - pdl), 2)
    s2 = round(pivot - (pdh - pdl), 2)
    r3 = round(pdh + 2 * (pivot - pdl), 2)
    s3 = round(pdl - 2 * (pdh - pivot), 2)
    r4 = round(r3 + (r2 - r1), 2)
    s4 = round(s3 - (s1 - s2), 2)

    # "average touch zone" - where price has been oscillating around today,
    # taken as a band around the pivot and today's spot (not the day's
    # actual high/low, which the frontend already shows separately).
    avg = round((pivot + spot) / 2, 2)
    zone_low = round(min(s1, avg - abs(spot - pivot) - 20), 2)
    zone_high = round(max(r1, avg + abs(spot - pivot) + 20), 2)

    return {
        "index": underlying,
        "pdc": pdc, "open": spot, "low": min(pdl, spot), "high": max(pdh, spot),
        "low_30": round(min(pdl, spot) - 20, 2), "high_30": round(max(pdh, spot) + 20, 2),
        "avg": avg, "pivot_close": pdc,
        "supports": {"S1": s1, "S2": s2, "S3": s3, "S4": s4},
        "resistances": {"R1": r1, "R2": r2, "R3": r3, "R4": r4},
        "zone": {"low": zone_low, "high": zone_high},
        "pdh": pdh, "pdl": pdl,
        "source": "live",
    }


def _get_zone_data(spot: float, underlying: str) -> dict:
    """Shared by /dashboard/nifty-zone and the Trade Map zone badge - real
    pivot zone first (live mode), mock as the fallback. Kept as one place so
    both features always agree on which zone NIFTY is actually in."""
    if USE_LIVE_MARKET_DATA:
        try:
            zone = _real_nifty_zone(spot, underlying)
            return zone
        except Exception as exc:
            logger.warning("Live nifty-zone FAILED for %s: %s - falling back to mock", underlying, exc)
            mock_zone = mock_data.generate_nifty_zone(spot, underlying)
            mock_zone["source"] = "mock"
            mock_zone["zone_live_error"] = str(exc)
            return mock_zone
    mock_zone = mock_data.generate_nifty_zone(spot, underlying)
    mock_zone["source"] = "mock"
    return mock_zone


def _nearest_zone_label(spot: float, zone: dict) -> dict:
    """Which real support/resistance level `spot` is closest to right now,
    e.g. {'label': 'Near R1', 'kind': 'resistance', 'bias': 'BULLISH'} - this
    is the actual real-data version of the static 'MARKET BULLISH S1'-style
    badge, instead of a hardcoded/repeated label."""
    levels = [("PC", zone.get("pivot_close"))]
    for k, v in (zone.get("supports") or {}).items():
        levels.append((k, v))
    for k, v in (zone.get("resistances") or {}).items():
        levels.append((k, v))
    levels = [(k, v) for k, v in levels if v is not None]
    if not levels:
        return {"label": None, "kind": None, "bias": None}
    nearest_key, nearest_val = min(levels, key=lambda kv: abs(kv[1] - spot))
    kind = "support" if nearest_key.startswith("S") else ("resistance" if nearest_key.startswith("R") else "pivot")
    pivot = zone.get("pivot_close") or nearest_val
    bias = "BULLISH" if spot >= pivot else "BEARISH"
    return {"label": "Near " + nearest_key, "kind": kind, "bias": bias}


@app.get("/dashboard/nifty-zone")
def nifty_zone(underlying: str = "NIFTY", current_user: models.User = Depends(auth.get_current_user)):
    overview = mock_data.generate_market_overview()
    overview = _live_index_overlay(overview)
    spot = _spot_for(underlying, overview)
    zone = _get_zone_data(spot, underlying)
    return {**zone, "disclaimer": DISCLAIMER}


@app.get("/dashboard/market-breadth")
def market_breadth(current_user: models.User = Depends(auth.get_current_user)):
    return {**mock_data.generate_market_breadth(), "disclaimer": DISCLAIMER}


@app.get("/dashboard/sector-performance")
def sector_performance(current_user: models.User = Depends(auth.get_current_user)):
    """Powers the sector %change strip on the Market View page."""
    return {"sectors": mock_data.generate_sector_performance(), "disclaimer": DISCLAIMER}


# ---------------------------------------------------------------------------
# OPTION CHAIN + BEST STRIKE FINDER (spec sections 5-6)
# ---------------------------------------------------------------------------
def _to_angel_expiry(expiry_iso: str) -> str:
    """'2026-09-25' -> '25SEP2026' (Angel One's own expiry-date format)."""
    dt = datetime.strptime(expiry_iso, "%Y-%m-%d")
    return dt.strftime("%d%b%Y").upper()


_FALLBACK_EXPIRY = "2026-09-25"  # only used in mock mode / if the live expiry lookup itself fails


def _resolve_expiry(underlying: str, expiry: str | None) -> str:
    """If the caller didn't pass an explicit expiry, resolve the REAL
    nearest listed expiry from Angel One (live mode) instead of a hardcoded
    date. A hardcoded expiry silently goes stale the moment that date
    passes (or if it was never a real listed expiry to begin with) - and
    Angel's optionGreek endpoint just returns 'No Data Available' for a
    date it doesn't recognise, which is why Greeks were never activating
    even with live mode on and valid credentials."""
    if expiry:
        return expiry
    if USE_LIVE_MARKET_DATA:
        try:
            return angel_one_client.get_nearest_expiry(underlying)
        except Exception as exc:
            logger.warning("Live nearest-expiry lookup FAILED for %s: %s - using fallback date", underlying, exc)
    return _FALLBACK_EXPIRY


@app.get("/options/expiries")
def option_expiries(underlying: str = "NIFTY", current_user: models.User = Depends(auth.get_current_user)):
    """Real list of Angel One's currently-listed expiries for `underlying`
    - powers an Expiry dropdown so the frontend never has to guess/hardcode
    a date. Falls back to a single mock date if live mode is off or the
    lookup fails."""
    if USE_LIVE_MARKET_DATA:
        try:
            expiries = angel_one_client.get_available_expiries(underlying)
            return {"expiries": expiries, "source": "live", "disclaimer": DISCLAIMER}
        except Exception as exc:
            logger.warning("Live expiries lookup FAILED for %s: %s", underlying, exc)
            return {"expiries": [_FALLBACK_EXPIRY], "source": "mock", "live_error": str(exc), "disclaimer": DISCLAIMER}
    return {"expiries": [_FALLBACK_EXPIRY], "source": "mock", "disclaimer": DISCLAIMER}


def _live_option_quote_overlay(chain: dict, underlying: str, expiry: str) -> dict:
    """Best-effort replace mock option LTP/OI/OI-change/volume with Angel One
    live quotes, then recompute Max Pain and PCR from those live OI values -
    otherwise those two fields would silently stay based on the original
    mock OI even after every per-strike number on the chain has gone live."""
    if not USE_LIVE_MARKET_DATA:
        return chain
    try:
        strikes = [c["strike"] for c in chain["calls"]]
        strikes += [p["strike"] for p in chain["puts"]]
        quotes = angel_one_client.get_option_quotes(underlying, expiry, strikes)
        if not quotes:
            return chain

        for option_type, rows in (("CE", chain["calls"]), ("PE", chain["puts"])):
            for option in rows:
                q = quotes.get((round(float(option["strike"]), 2), option_type))
                if not q:
                    continue
                if q.get("ltp") is not None:
                    option["ltp"] = q["ltp"]
                if q.get("oi") is not None:
                    option["oi"] = q["oi"]
                if q.get("oi_change_pct") is not None:
                    option["oi_change_pct"] = q["oi_change_pct"]
                if q.get("volume") is not None:
                    option["volume"] = q["volume"]
                option["option_source"] = "live"
                option["symboltoken"] = q["symboltoken"]
                option["tradingsymbol"] = q["tradingsymbol"]

        chain["option_quotes_live"] = True
        overall_bias, overall_final_signal = mock_data.compute_oi_bias(chain["calls"], chain["puts"])
        chain["overall_oi_bias"] = overall_bias
        chain["overall_final_signal"] = overall_final_signal
        max_pain, pcr = mock_data.compute_max_pain_and_pcr(chain["calls"], chain["puts"])
        chain["max_pain"] = max_pain
        chain["pcr"] = pcr

        # Real multi-timeframe confirmation: record this actual live-OI
        # reading, then read back the ACTUAL bias from 3/5/15/30 minutes
        # ago (not a random guess) - see oi_history.py. Timeframes with
        # no history yet come back as None; the frontend shows those as
        # "Building..." instead of inventing a value.
        oi_history.record(underlying, expiry, overall_bias)
        chain["timeframe_oi_bias"] = oi_history.timeframe_bias(underlying, expiry, overall_bias)
        chain["timeframe_oi_bias_source"] = "live_history"
        return chain
    except Exception as exc:
        chain["option_quotes_live"] = False
        chain["option_quotes_live_error"] = str(exc)
        logger.warning("Live option quotes FAILED for %s %s: %s", underlying, expiry, exc)
        return chain


def _live_greeks_overlay(chain: dict, underlying: str, expiry: str) -> dict:
    """Best-effort: overwrite the mock Delta/Gamma/Theta/Vega/IV on each
    strike with Angel One's real Option Greeks endpoint (one call covers
    every strike+CE/PE for that expiry). OI/LTP/volume/OI-change are handled
    separately by _live_option_quote_overlay() right after this one (a
    different endpoint, per-contract token+quote lookup) - "bias"/"final_signal"
    stay derived from whichever OI values end up on the chain (mock or live)
    by generate_mock_option_chain(), not overwritten here. Any mismatch or failure leaves the mock numbers untouched."""
    if not USE_LIVE_MARKET_DATA:
        return chain
    try:
        rows = angel_one_client.get_option_greeks(underlying, _to_angel_expiry(expiry))
        logger.info("Live Option Greeks OK for %s %s: %d rows", underlying, expiry, len(rows))
    except Exception as exc:
        chain["greeks_live_error"] = str(exc)
        logger.warning("Live Option Greeks FAILED for %s %s: %s", underlying, expiry, exc)
        return chain
    by_key = {(round(float(r["strikePrice"])), r["optionType"]): r for r in rows}
    for c in chain["calls"]:
        r = by_key.get((c["strike"], "CE"))
        if r:
            c["delta"], c["gamma"], c["theta"], c["vega"], c["iv"] = (
                float(r["delta"]), float(r["gamma"]), float(r["theta"]), float(r["vega"]), float(r["impliedVolatility"]),
            )
            c["greeks_source"] = "live"
    for p in chain["puts"]:
        r = by_key.get((p["strike"], "PE"))
        if r:
            p["delta"], p["gamma"], p["theta"], p["vega"], p["iv"] = (
                float(r["delta"]), float(r["gamma"]), float(r["theta"]), float(r["vega"]), float(r["impliedVolatility"]),
            )
            p["greeks_source"] = "live"
    chain["greeks_live"] = True
    return chain


def _attach_oi_cluster(chain: dict) -> dict:
    """Chain-level OI Cluster S/R badge - same rule engine find_best_trade()
    already uses internally, exposed here so any endpoint that hands back
    a chain (not just /aitrade/best-trade) carries oi_cluster_signal /
    oi_cluster_confirmation / oi_cluster_direction for the frontend to
    render a badge. Works in mock mode too (the strategy only needs the
    chain's own calls/puts/max_pain/pcr, whichever data - mock or live -
    ended up on it)."""
    cluster = probability.get_oi_cluster_signal(chain)
    chain["oi_cluster_signal"] = cluster["reason"] if cluster else None
    chain["oi_cluster_confirmation"] = cluster["confirmation"] if cluster else None
    chain["oi_cluster_direction"] = cluster["direction"] if cluster else None
    return chain


@app.get("/options/chain")
def option_chain(
    underlying: str = "NIFTY",
    expiry: str | None = None,
    current_user: models.User = Depends(auth.get_current_user),
):
    expiry = _resolve_expiry(underlying, expiry)
    spot = _resolve_spot(underlying)
    chain = mock_data.generate_mock_option_chain(underlying, spot, expiry)
    chain = _live_greeks_overlay(chain, underlying, expiry)
    chain = _live_option_quote_overlay(chain, underlying, expiry)
    chain = _attach_oi_cluster(chain)

    # Same scoring evaluate_strike_candidate() uses for the best-setup pick,
    # applied to every strike here too - so any consumer of the full chain
    # (e.g. the dashboard's Option Chain preview card) can show a real,
    # consistently-computed probability per strike instead of a separate
    # made-up number.
    direction_snapshot = mock_data.generate_stock_snapshot(underlying)
    underlying_result = probability.score_stock(direction_snapshot)
    bearish_probability = round(100 - underlying_result.probability, 1)
    for c in chain["calls"]:
        c["probability"] = probability.evaluate_strike_candidate(c, "CE", underlying_result.probability, spot)["probability"]
    for p in chain["puts"]:
        p["probability"] = probability.evaluate_strike_candidate(p, "PE", bearish_probability, spot)["probability"]

    return {**chain, "disclaimer": DISCLAIMER}


def _compute_best_setup(underlying: str, expiry: str) -> dict:
    """Shared by /options/best-setup and /aitrade/ai-analysis - computes the
    OI-first best CE/PE pick plus the underlying-direction probability, so
    the AI layer reasons over the exact same numbers the dashboard shows."""
    spot = _resolve_spot(underlying)
    chain = mock_data.generate_mock_option_chain(underlying, spot, expiry)
    # Keep best-setup selection on the same live option values shown by the
    # dashboard: Greeks + option LTP/OI/volume overlays are applied before
    # any strike scoring or recommendation is calculated.
    chain = _live_greeks_overlay(chain, underlying, expiry)
    chain = _live_option_quote_overlay(chain, underlying, expiry)
    chain = _attach_oi_cluster(chain)

    # crude underlying-direction probability, reused from the stock engine's shape
    direction_snapshot = mock_data.generate_stock_snapshot(underlying)
    underlying_result = probability.score_stock(direction_snapshot)

    # Step 1 - OPTION CHAIN FIRST: only strikes where real OI buildup (the "big
    # value", not just %) already favours that side get considered at all; among
    # those, take the one with the biggest absolute OI value added.
    # Step 2 - indicator/AI is applied AFTER, only to score/explain the pick -
    # it never overrides which strike the OI data chose.
    oi_favored_calls = [c for c in chain["calls"] if c["final_signal"] == "CALL BUY"]
    best_call = max(oi_favored_calls or chain["calls"], key=lambda c: abs(c["oi_change_value"]))

    oi_favored_puts = [p for p in chain["puts"] if p["final_signal"] == "PUT BUY"]
    best_put = max(oi_favored_puts or chain["puts"], key=lambda p: abs(p["oi_change_value"]))

    # Same scoring function find_best_trade() uses per-candidate - real
    # ATR-based targets, a real risk_reward number (not a hardcoded string),
    # and the liquidity/moneyness/IV checks, instead of a separate naive
    # entry*1.1/1.2/1.3 calc that skipped all of that.
    def build_setup(option, option_type, direction_probability):
        result = probability.evaluate_strike_candidate(option, option_type, direction_probability, spot)
        return {"symbol": underlying, "expiry": expiry, **result}

    zone_label = None
    if underlying in _INDEX_UNDERLYINGS:
        zone = _get_zone_data(spot, underlying)
        zone_label = _nearest_zone_label(spot, zone)

    return {
        "spot": spot,
        "chain": chain,
        "zone_label": zone_label,
        "underlying_probability": underlying_result.probability,
        "underlying_reasons": underlying_result.reasons,
        "best_ce": build_setup(best_call, "CE", underlying_result.probability),
        "best_pe": build_setup(best_put, "PE", 100 - underlying_result.probability),
        "max_pain": chain.get("max_pain"),
        "pcr": chain.get("pcr"),
        "oi_cluster_signal": chain.get("oi_cluster_signal"),
        "oi_cluster_confirmation": chain.get("oi_cluster_confirmation"),
        "oi_cluster_direction": chain.get("oi_cluster_direction"),
    }


@app.get("/options/best-setup")
def best_setup(
    underlying: str = "NIFTY",
    expiry: str | None = None,
    current_user: models.User = Depends(auth.get_current_user),
):
    expiry = _resolve_expiry(underlying, expiry)
    result = _compute_best_setup(underlying, expiry)
    return {
        "best_ce": result["best_ce"],
        "best_pe": result["best_pe"],
        "max_pain": result["max_pain"],
        "pcr": result["pcr"],
        "oi_cluster_signal": result["oi_cluster_signal"],
        "oi_cluster_confirmation": result["oi_cluster_confirmation"],
        "oi_cluster_direction": result["oi_cluster_direction"],
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# MARKET SCANNER (multi-timeframe zones + EMA/MACD/RSI aligned stock lists)
# ---------------------------------------------------------------------------
@app.get("/scanner/overview")
def scanner_overview(current_user: models.User = Depends(auth.get_current_user)):
    zones = mock_data.generate_timeframe_zones()
    adv_decl = mock_data.generate_advance_decline()

    snapshots = mock_data.generate_universe_snapshot()
    all_up, all_down = [], []
    for s in snapshots:
        alignment = probability.classify_indicator_alignment(s)
        if alignment == "BULLISH":
            all_up.append(s)
        elif alignment == "BEARISH":
            all_down.append(s)

    def trim(rows):
        return [
            {
                "symbol": r["symbol"], "sector": r["sector"], "ltp": r["ltp"], "change_pct": r["change_pct"],
                "volume": r["volume"], "rsi": r["rsi"], "pdh": r["pdh"],
            }
            for r in rows[:15]
        ]

    return {
        "timeframe_zones": zones,
        "advance_decline": adv_decl,
        "all_up_stocks": trim(all_up),
        "all_down_stocks": trim(all_down),
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# AI TRADE FINDER (spec: independent CE/PE analysis, strike selection engine,
# no-trade condition - never forces a recommendation)
# ---------------------------------------------------------------------------
@app.get("/aitrade/best-trade")
def ai_trade_finder(
    underlying: str = "NIFTY",
    expiry: str | None = None,
    current_user: models.User = Depends(auth.get_current_user),
):
    expiry = _resolve_expiry(underlying, expiry)
    spot = _resolve_spot(underlying)
    chain = mock_data.generate_mock_option_chain(underlying, spot, expiry)
    # Use the same live option data as /options/chain and /options/best-setup.
    chain = _live_greeks_overlay(chain, underlying, expiry)
    chain = _live_option_quote_overlay(chain, underlying, expiry)

    direction_snapshot = mock_data.generate_stock_snapshot(underlying)
    underlying_result = probability.score_stock(direction_snapshot)

    result = probability.find_best_trade(chain, underlying_result.probability)
    zone_label = None
    if underlying in _INDEX_UNDERLYINGS:
        zone = _get_zone_data(spot, underlying)
        zone_label = _nearest_zone_label(spot, zone)
    return {
        "underlying": underlying,
        "spot": spot,
        "expiry": expiry,
        "zone_label": zone_label,
        "max_pain": chain.get("max_pain"),
        "pcr": chain.get("pcr"),
        **result,
        "disclaimer": DISCLAIMER,
    }


@app.get("/aitrade/ai-analysis")
def ai_trade_analysis(
    underlying: str = "NIFTY",
    expiry: str | None = None,
    current_user: models.User = Depends(auth.get_current_user),
):
    """Claude-powered second opinion on top of the OI-first rule engine
    (see _compute_best_setup). The LLM never picks the strike - it only
    reviews the engine's own pick against the indicator reasons and
    (currently mock) news, and returns a plain-English verdict, a
    confidence label, and what it sees as the biggest risk."""
    expiry = _resolve_expiry(underlying, expiry)
    if not ai_engine.is_configured():
        return {
            "ai_available": False,
            "message": (
                "AI analysis is not set up yet. Set GEMINI_API_KEY (free "
                "tier - get one at https://aistudio.google.com/apikey) or "
                "ANTHROPIC_API_KEY as an environment variable on the "
                "server to enable it."
            ),
            "disclaimer": DISCLAIMER,
        }

    setup = _compute_best_setup(underlying, expiry)
    news = mock_data.generate_mock_news(underlying)
    context = {
        "underlying": underlying,
        "spot": setup["spot"],
        "underlying_probability": setup["underlying_probability"],
        "underlying_reasons": setup["underlying_reasons"],
        "overall_oi_bias": setup["chain"]["overall_oi_bias"],
        "overall_final_signal": setup["chain"]["overall_final_signal"],
        "best_ce": setup["best_ce"],
        "best_pe": setup["best_pe"],
        "news": news,
    }
    try:
        analysis = ai_engine.generate_trade_analysis(context)
    except Exception as exc:
        logger.warning("AI analysis FAILED for %s: %s", underlying, exc)
        return {
            "ai_available": False,
            "message": "Could not get an AI analysis right now: " + str(exc),
            "disclaimer": DISCLAIMER,
        }

    return {
        "ai_available": True,
        "underlying": underlying,
        "favored_side": setup["chain"]["overall_final_signal"],
        "best_ce": setup["best_ce"],
        "best_pe": setup["best_pe"],
        "news": news,
        "analysis": analysis,
        "disclaimer": DISCLAIMER + " AI analysis is generated by a language model and can be wrong - it is a second opinion on the rule engine's pick, not financial advice.",
    }


# ---------------------------------------------------------------------------
# SIGNAL LOGGING + BACKTESTING (spec sections 9-10, 14)
# ---------------------------------------------------------------------------
@app.post("/signals/log", response_model=schemas.SignalLogOut)
def log_signal(
    symbol: str,
    instrument_type: str,
    direction: str,
    entry_price: float,
    target_1: float,
    stop_loss: float,
    predicted_probability: float,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Records a signal at the moment it's generated. This is what
    later lets the backtest engine compare predicted probability to
    actual win rate (spec section 10)."""
    entry = models.SignalLog(
        symbol=symbol,
        instrument_type=instrument_type,
        direction=direction,
        entry_price=entry_price,
        target_1=target_1,
        stop_loss=stop_loss,
        predicted_probability=predicted_probability,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@app.get("/signals/history", response_model=list[schemas.SignalLogOut])
def signal_history(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    return db.query(models.SignalLog).order_by(models.SignalLog.created_at.desc()).limit(200).all()


@app.get("/signals/backtest-summary")
def backtest_summary(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    """Groups closed signals into probability buckets and shows actual
    win rate per bucket (spec section 10). If no real signals have been
    logged yet, falls back to a clearly-labeled demo calibration report
    so the page has something meaningful to show in mock mode."""
    closed = db.query(models.SignalLog).filter(models.SignalLog.result.isnot(None)).all()

    if not closed:
        return {**mock_data.generate_mock_backtest_summary(), "disclaimer": DISCLAIMER}

    buckets = {f"{low}-{low+5}%": {"total": 0, "wins": 0} for low in range(50, 90, 5)}
    for signal in closed:
        low = int(signal.predicted_probability // 5) * 5
        key = f"{low}-{low+5}%"
        if key in buckets:
            buckets[key]["total"] += 1
            if signal.result == "WIN":
                buckets[key]["wins"] += 1

    summary = {}
    total_trades = 0
    total_wins = 0
    for key, data in buckets.items():
        win_rate = round((data["wins"] / data["total"]) * 100, 1) if data["total"] else None
        summary[key] = {"total_trades": data["total"], "wins": data["wins"], "actual_win_rate": win_rate}
        total_trades += data["total"]
        total_wins += data["wins"]

    return {
        "is_demo_data": False,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "overall_win_rate": round(total_wins / total_trades * 100, 1) if total_trades else 0,
        "buckets": summary,
        "disclaimer": DISCLAIMER,
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "mode": "LIVE" if USE_LIVE_MARKET_DATA else "MOCK_DATA",
        "angel_one_configured": angel_one_client.is_configured(),
        "time": datetime.utcnow(),
    }
