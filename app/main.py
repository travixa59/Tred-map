from datetime import datetime
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from . import models, schemas, auth, mock_data, probability
from .database import engine, get_db

models.Base.metadata.create_all(bind=engine)

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
    return {**mock_data.generate_market_overview(), "disclaimer": DISCLAIMER}


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


@app.get("/dashboard/nifty-zone")
def nifty_zone(underlying: str = "NIFTY", current_user: models.User = Depends(auth.get_current_user)):
    overview = mock_data.generate_market_overview()
    spot = _spot_for(underlying, overview)
    return {**mock_data.generate_nifty_zone(spot, underlying), "disclaimer": DISCLAIMER}


@app.get("/dashboard/market-breadth")
def market_breadth(current_user: models.User = Depends(auth.get_current_user)):
    return {**mock_data.generate_market_breadth(), "disclaimer": DISCLAIMER}


# ---------------------------------------------------------------------------
# OPTION CHAIN + BEST STRIKE FINDER (spec sections 5-6)
# ---------------------------------------------------------------------------
@app.get("/options/chain")
def option_chain(
    underlying: str = "NIFTY",
    expiry: str = "2026-09-25",
    current_user: models.User = Depends(auth.get_current_user),
):
    overview = mock_data.generate_market_overview()
    spot = _spot_for(underlying, overview)
    chain = mock_data.generate_mock_option_chain(underlying, spot, expiry)
    return {**chain, "disclaimer": DISCLAIMER}


@app.get("/options/best-setup")
def best_setup(
    underlying: str = "NIFTY",
    expiry: str = "2026-09-25",
    current_user: models.User = Depends(auth.get_current_user),
):
    overview = mock_data.generate_market_overview()
    spot = _spot_for(underlying, overview)
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
    overview = mock_data.generate_market_overview()
    spot = _spot_for(underlying, overview)
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
    return {"status": "ok", "mode": "MOCK_DATA", "time": datetime.utcnow()}
