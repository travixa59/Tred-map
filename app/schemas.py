from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr


# ---- auth ----
class UserCreate(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---- signals ----
class StockSignal(BaseModel):
    rank: int
    symbol: str
    ltp: float
    change_pct: float
    probability: float
    trend: str
    rsi: float
    volume: int


class OptionSetup(BaseModel):
    symbol: str
    strike: float
    option_type: str  # CE / PE
    expiry: str
    probability: float
    buy_range_low: float
    buy_range_high: float
    target_1: float
    target_2: float
    target_3: float
    stop_loss: float
    risk_reward: str
    reasons: list[str]


class SignalLogOut(BaseModel):
    id: int
    created_at: datetime
    symbol: str
    instrument_type: str
    direction: str
    entry_price: float
    target_1: float
    stop_loss: float
    predicted_probability: float
    status: str
    result: Optional[str] = None

    class Config:
        from_attributes = True
