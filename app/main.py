from datetime import datetime
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from . import models, schemas, auth, mock_data, probability, angel_one_client
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


def _live_commodity_overlay(commodities: dict) -> dict:
    return _live_overlay(commodities, [("GOLD", "GOLD"), ("SILVER", "SILVER"), ("CRUDEOIL", "CRUDEOIL"), ("COPPER", "COPPER")])


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


@app.get("/dashboard/commodities")
def commodity_overview(current_user: models.User = Depends(auth.get_current_user)):
    commodities = mock_data.generate_commodity_overview()
    commodities = _live_commodity_overlay(commodities)
    return {**commodities, "disclaimer": DISCLAIMER, "live_data": USE_LIVE_MARKET_DATA}


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


@app.get("/dashboard/nifty-zone")
def nifty_zone(underlying: str = "NIFTY", current_user: models.User = Depends(auth.get_current_user)):
    overview = mock_data.generate_market_overview()
    overview = _live_index_overlay(overview)
    spot = _spot_for(underlying, overview)
    return {**mock_data.generate_nifty_zone(spot, underlying), "disclaimer": DISCLAIMER}


@app.get("/dashboard/market-breadth")
def market_breadth(current_user: models.User = Depends(auth.get_current_user)):
    return {**mock_data.generate_market_breadth(), "disclaimer": DISCLAIMER}


# ---------------------------------------------------------------------------
# OPTION CHAIN + BEST STRIKE FINDER (spec sections 5-6)
# ---------------------------------------------------------------------------
def _to_angel_expiry(expiry_iso: str) -> str:
    """'2026-09-25' -> '25SEP2026' (Angel One's own expiry-date format)."""
    dt = datetime.strptime(expiry_iso, "%Y-%m-%d")
    return dt.strftime("%d%b%Y").upper()


def _live_greeks_overlay(chain: dict, underlying: str, expiry: str) -> dict:
    """Best-effort: overwrite the mock Delta/Gamma/Theta/Vega/IV on each
    strike with Angel One's real Option Greeks endpoint (one call covers
    every strike+CE/PE for that expiry). OI/bias/LTP are left as mock for
    now - that needs a separate per-contract token+quote lookup, a later
    step. Any mismatch or failure leaves the mock numbers untouched."""
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


@app.get("/options/chain")
def option_chain(
    underlying: str = "NIFTY",
    expiry: str = "2026-09-25",
    current_user: models.User = Depends(auth.get_current_user),
):
    spot = _resolve_spot(underlying)
    chain = mock_data.generate_mock_option_chain(underlying, spot, expiry)
    chain = _live_greeks_overlay(chain, underlying, expiry)
    return {**chain, "disclaimer": DISCLAIMER}


@app.get("/options/best-setup")
def best_setup(
    underlying: str = "NIFTY",
    expiry: str = "2026-09-25",
    current_user: models.User = Depends(auth.get_current_user),
):
    spot = _resolve_spot(underlying)
    chain = mock_data.generate_mock_option_chain(underlying, spot, expiry)

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
    call_score = probability.score_option("BULLISH", best_call["oi_change_pct"], best_call["iv"], underlying_result.probability)

    oi_favored_puts = [p for p in chain["puts"] if p["final_signal"] == "PUT BUY"]
    best_put = max(oi_favored_puts or chain["puts"], key=lambda p: abs(p["oi_change_value"]))
    put_score = probability.score_option("BEARISH", best_put["oi_change_pct"], best_put["iv"], 100 - underlying_result.probability)

    def build_setup(option, score, option_type):
        entry = option["ltp"]
        return {
            "symbol": underlying,
            "strike": option["strike"],
            "option_type": option_type,
            "expiry": expiry,
            "probability": score.probability,
            "buy_range_low": round(entry * 0.98, 2),
            "buy_range_high": round(entry * 1.02, 2),
            "target_1": round(entry * 1.10, 2),
            "target_2": round(entry * 1.20, 2),
            "target_3": round(entry * 1.30, 2),
            "stop_loss": round(entry * 0.90, 2),
            "risk_reward": "1:2+",
            "reasons": score.reasons,
        }

    return {
        "best_ce": build_setup(best_call, call_score, "CE"),
        "best_pe": build_setup(best_put, put_score, "PE"),
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
                "symbol": r["symbol"], "ltp": r["ltp"], "change_pct": r["change_pct"],
                "volume": r["volume"], "rsi": r["rsi"],
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
    expiry: str = "2026-09-25",
    current_user: models.User = Depends(auth.get_current_user),
):
    spot = _resolve_spot(underlying)
    chain = mock_data.generate_mock_option_chain(underlying, spot, expiry)

    direction_snapshot = mock_data.generate_stock_snapshot(underlying)
    underlying_result = probability.score_stock(direction_snapshot)

    result = probability.find_best_trade(chain, underlying_result.probability)
    return {
        "underlying": underlying,
        "spot": spot,
        "expiry": expiry,
        **result,
        "disclaimer": DISCLAIMER,
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
