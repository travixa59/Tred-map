"""
Angel One SmartAPI client.

This is the ONLY file that talks to the real broker. Nothing else in the
app should import `requests`/`pyotp` directly - main.py just calls the
functions here, so mock_data.py can stay as the offline fallback forever.

--------------------------------------------------------------------------
SETUP (do this once, tonight, before tomorrow's trial):
--------------------------------------------------------------------------
1. pip install requests pyotp

2. You need an Angel One trading account (if you don't have one yet, this
   step alone can take a day or more for KYC - do this first).

3. Log into https://smartapi.angelone.in with your Angel One credentials,
   create an "app", and you'll get:
       - API_KEY      (called "X-PrivateKey" in requests)
       - SECRET_KEY   (not needed for the plain login flow used here)

4. In the Angel One mobile app, set up TOTP-based 2FA if you haven't
   already (Profile -> Settings -> enable 2FA / Authenticator). You'll
   get a QR code - scan it once with Google Authenticator etc. to grab
   the underlying secret (the same secret can be typed into `pyotp`
   instead of scanning a code every time).

5. Set these as environment variables before starting the FastAPI app -
   NEVER hardcode them in this file or commit them to git:

       ANGEL_API_KEY=xxxxxxxx
       ANGEL_CLIENT_CODE=your_client_code
       ANGEL_PIN=your_4-digit_pin
       ANGEL_TOTP_SECRET=your_totp_secret   (the raw secret, not a 6-digit code)
       USE_LIVE_MARKET_DATA=true

   With USE_LIVE_MARKET_DATA unset or "false", the app keeps using
   mock_data.py exactly as before - nothing breaks if you don't have
   credentials yet.

--------------------------------------------------------------------------
KNOWN GAPS - things this file does NOT do yet (next small steps):
--------------------------------------------------------------------------
- Per-strike OI / option LTP (needs a symbol-token lookup per contract
  from the scrip master, then a batch quote call). Right now only the
  INDEX spot price and the Option Greeks endpoint are wired to real
  data; OI/bias in the option chain still comes from mock_data.py.
- ADX / RSI on the ticker are still mock - Angel One doesn't hand you
  those directly, they need candle history + your own indicator math.
- No response caching/retry/backoff yet - fine for a one-screen trial
  tomorrow, not for production load.
- Exact request/response shapes for anything beyond login, LTP and
  optionGreek should be double-checked against the current SmartAPI
  docs (https://smartapi.angelone.in) when you actually run this, since
  broker APIs do change their contracts over time.
"""

import os
import time
import requests
import pyotp

API_KEY = os.environ.get("ANGEL_API_KEY", "")
CLIENT_CODE = os.environ.get("ANGEL_CLIENT_CODE", "")
PIN = os.environ.get("ANGEL_PIN", "")
TOTP_SECRET = os.environ.get("ANGEL_TOTP_SECRET", "")

BASE_URL = "https://apiconnect.angelone.in"
SCRIP_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"

_session = {"jwt_token": None, "feed_token": None, "expires_at": 0}
_scrip_master_cache = None  # loaded lazily, kept in memory for the process lifetime


def is_configured() -> bool:
    """True once all 4 credential env vars are set - main.py checks this
    before attempting any live call, so a half-configured setup just
    falls back to mock data instead of crashing a request."""
    return bool(API_KEY and CLIENT_CODE and PIN and TOTP_SECRET)


def _headers(auth: bool = True) -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00",
        "X-PrivateKey": API_KEY,
    }
    if auth and _session["jwt_token"]:
        h["Authorization"] = "Bearer " + _session["jwt_token"]
    return h


def login() -> None:
    """Logs in with clientcode + pin + a freshly-generated TOTP code.
    Angel One sessions last until ~midnight, so we just re-login whenever
    we don't have a token yet or the cached one has gone stale."""
    if not is_configured():
        raise RuntimeError(
            "Angel One credentials are not set. Set ANGEL_API_KEY, "
            "ANGEL_CLIENT_CODE, ANGEL_PIN and ANGEL_TOTP_SECRET as "
            "environment variables first."
        )
    totp_code = pyotp.TOTP(TOTP_SECRET).now()
    resp = requests.post(
        BASE_URL + "/rest/auth/angelbroking/user/v1/loginByPassword",
        json={"clientcode": CLIENT_CODE, "password": PIN, "totp": totp_code},
        headers=_headers(auth=False),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("status"):
        raise RuntimeError("Angel One login failed: " + str(data.get("message")))
    _session["jwt_token"] = data["data"]["jwtToken"]
    _session["feed_token"] = data["data"].get("feedToken")
    _session["expires_at"] = time.time() + 60 * 60 * 6  # re-login every 6h to be safe


def _ensure_session() -> None:
    if not _session["jwt_token"] or time.time() > _session["expires_at"]:
        login()


def _load_scrip_master() -> list:
    """Downloads Angel One's full instrument list once per process. This is
    a big JSON file (tens of MB) - fine to fetch on first use and keep in
    memory, not something to call per-request."""
    global _scrip_master_cache
    if _scrip_master_cache is None:
        resp = requests.get(SCRIP_MASTER_URL, timeout=30)
        resp.raise_for_status()
        _scrip_master_cache = resp.json()
    return _scrip_master_cache


def find_symbol_token(exch_seg: str, name_candidates) -> dict | None:
    """Looks up a symbol token by exchange segment + name. `name_candidates`
    can be a single string or a list of possible values to try in order -
    Angel One's scrip master naming isn't perfectly predictable across
    indices, so we try a couple of reasonable variants instead of hardcoding
    exactly one and failing silently on a mismatch."""
    if isinstance(name_candidates, str):
        name_candidates = [name_candidates]
    rows = _load_scrip_master()
    wanted = {n.upper() for n in name_candidates}
    for row in rows:
        if row.get("exch_seg") == exch_seg and str(row.get("name", "")).upper() in wanted:
            return row
    return None


# Index name -> (exchange segment, [candidate scrip-master "name" values]) for
# spot LTP lookup. NOTE: Angel One's scrip master uses SHORT codes in the
# "name" field (e.g. {"symbol":"Nifty 50","name":"NIFTY","exch_seg":"NSE",...})
# - the longer, spaced-out label lives in "symbol", not "name". Matching on
# "NIFTY 50" (with a space) instead of "NIFTY" was the original bug here.
_INDEX_LOOKUP = {
    "NIFTY": ("NSE", ["NIFTY"]),
    "BANKNIFTY": ("NSE", ["BANKNIFTY", "NIFTY BANK", "NIFTYBANK"]),
    "SENSEX": ("BSE", ["SENSEX"]),
}


_ltp_cache = {}  # underlying -> (fetched_at_epoch_seconds, {"ltp":..., "change_pct":...})
_LTP_CACHE_TTL_SECONDS = 5  # short-lived: keeps a page load's several calls to the
# same index from each hitting Angel One separately, without going stale for a trial


def get_index_ltp(underlying: str) -> dict:
    """Real spot LTP + change% for NIFTY / BANKNIFTY / SENSEX.
    Falls back to raising if the underlying isn't in _INDEX_LOOKUP -
    callers should catch and fall back to mock_data in that case.
    Cached for a few seconds so one page load (which can call this for the
    same underlying from several endpoints - overview, nifty-zone, chain)
    doesn't fire off several redundant real HTTP round-trips."""
    cached = _ltp_cache.get(underlying)
    if cached and (time.time() - cached[0]) < _LTP_CACHE_TTL_SECONDS:
        return cached[1]
    _ensure_session()
    exch_seg, name = _INDEX_LOOKUP[underlying]
    row = find_symbol_token(exch_seg, name)
    if row is None:
        raise RuntimeError(f"Could not find a symbol token for {underlying} in the scrip master.")
    resp = requests.post(
        BASE_URL + "/rest/secure/angelbroking/order/v1/getLtpData",
        json={"exchange": exch_seg, "tradingsymbol": row["symbol"], "symboltoken": row["token"]},
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("status"):
        raise RuntimeError("getLtpData failed: " + str(data.get("message")))
    d = data["data"]
    ltp = float(d["ltp"])
    close = float(d.get("close") or ltp)
    change_pct = round(((ltp - close) / close) * 100, 2) if close else 0.0
    result = {"ltp": ltp, "change_pct": change_pct}
    _ltp_cache[underlying] = (time.time(), result)
    return result


def get_option_greeks(name: str, expirydate: str) -> list:
    """Real Delta/Gamma/Theta/Vega/IV for every strike of `name` at
    `expirydate` (format like '25SEP2026', matching Angel One's own expiry
    naming) via the dedicated Option Greeks endpoint - one call covers the
    whole chain, no per-strike token lookup needed for this part."""
    _ensure_session()
    resp = requests.post(
        BASE_URL + "/rest/secure/angelbroking/marketData/v1/optionGreek",
        json={"name": name, "expirydate": expirydate},
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("status"):
        raise RuntimeError("optionGreek failed: " + str(data.get("message")))
    return data["data"]  # list of {name, expiry, strikePrice, optionType, delta, gamma, theta, vega, impliedVolatility, tradeVolume}
