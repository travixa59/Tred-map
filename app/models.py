"""
Database tables.

User        -> login accounts (spec section 20: Authentication)
SignalLog   -> every signal the app generates, plus its eventual outcome.
               This is the "diary" that powers the backtesting engine
               (spec sections 9-10: Historical Backtesting & Calibration).
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean
from .database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class SignalLog(Base):
    __tablename__ = "signal_logs"

    id = Column(Integer, primary_key=True, index=True)

    # --- what the signal was ---
    created_at = Column(DateTime, default=datetime.utcnow)
    symbol = Column(String, index=True)              # e.g. "RELIANCE" or "NIFTY"
    instrument_type = Column(String)                  # "STOCK", "CE", "PE"
    strike = Column(Float, nullable=True)              # only for options
    expiry = Column(String, nullable=True)              # only for options
    direction = Column(String)                          # "BULLISH" / "BEARISH"

    entry_price = Column(Float)
    target_1 = Column(Float)
    target_2 = Column(Float, nullable=True)
    target_3 = Column(Float, nullable=True)
    stop_loss = Column(Float)
    predicted_probability = Column(Float)               # 0-100

    # --- what actually happened (filled in later) ---
    status = Column(String, default="OPEN")             # OPEN, TARGET_HIT, SL_HIT, EXPIRED
    exit_price = Column(Float, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    result = Column(String, nullable=True)               # "WIN" / "LOSS"

    is_mock = Column(Boolean, default=True)              # True while in MOCK DATA MODE
