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
import json
import tempfile

API_KEY = os.environ.get("ANGEL_API_KEY", "")
CLIENT_CODE = os.environ.get("ANGEL_CLIENT_CODE", "")
PIN = os.environ.get("ANGEL_PIN", "")
TOTP_SECRET = os.environ.get("ANGEL_TOTP_SECRET", "")

BASE_URL = "https://apiconnect.angelone.in"
SCRIP_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
SCRIP_MASTER_DISK_CACHE = os.path.join(tempfile.gettempdir(), "angel_scrip_master.json")
SCRIP_MASTER_MAX_AGE_SECONDS = 24 * 60 * 60  # instrument list changes rarely - a day-old copy is fine

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
    """Loads Angel One's full instrument list (needed for individual stock
    F&O, since - unlike the 3 major indices - stock tokens aren't small
    enough in number to hand-hardcode). This file is tens of MB, so it's
    cached to LOCAL DISK (not just in-memory) - Render's free tier recycles
    the process on almost every spin-down/wake cycle, and re-downloading
    this from scratch every time was the exact bottleneck that made the app
    feel slow/stuck earlier. A same-day disk copy is reused instead; only a
    missing or >24h-old cache triggers a fresh download."""
    global _scrip_master_cache
    if _scrip_master_cache is not None:
        return _scrip_master_cache
    if os.path.exists(SCRIP_MASTER_DISK_CACHE):
        age = time.time() - os.path.getmtime(SCRIP_MASTER_DISK_CACHE)
        if age < SCRIP_MASTER_MAX_AGE_SECONDS:
            with open(SCRIP_MASTER_DISK_CACHE, "r") as f:
                _scrip_master_cache = json.load(f)
            return _scrip_master_cache
    resp = requests.get(SCRIP_MASTER_URL, timeout=30)
    resp.raise_for_status()
    _scrip_master_cache = resp.json()
    try:
        with open(SCRIP_MASTER_DISK_CACHE, "w") as f:
            json.dump(_scrip_master_cache, f)
    except OSError:
        pass  # disk cache is a nice-to-have; a failed write shouldn't break the request
    return _scrip_master_cache


# Index name -> (exchange segment, tradingsymbol, symboltoken) - HARDCODED.
# These are Angel One's own well-known, stable index tokens (confirmed from
# their public "real-time market data for 120 indices" release notes and
# historical-data docs), used directly instead of looking them up in the
# scrip master. That master file is tens of MB and was the actual cause of
# the app feeling slow/stuck: Render's free tier recycles the process on
# every spin-down/wake cycle, so the whole file was getting re-downloaded
# and re-parsed on almost every request burst. Skipping it entirely for
# just these 3 indices removes that bottleneck completely.
_INDEX_TOKENS = {
    "NIFTY": ("NSE", "Nifty 50", "99926000"),
    "BANKNIFTY": ("NSE", "Nifty Bank", "99926009"),
    "SENSEX": ("BSE", "SENSEX", "99919000"),
}

def find_symbol_token(exch_seg: str, name_candidates) -> dict | None:
    """Looks up a symbol token by exchange segment + name in the FULL scrip
    master. Kept as a fallback/general-purpose lookup (e.g. for individual
    option contracts later) - get_index_ltp no longer uses this for the 3
    indices, since _INDEX_TOKENS above is faster and avoids downloading the
    whole file just for 3 well-known constants. `name_candidates` can be a
    single string or a list of possible values to try in order."""
    if isinstance(name_candidates, str):
        name_candidates = [name_candidates]
    rows = _load_scrip_master()
    wanted = {n.upper() for n in name_candidates}
    for row in rows:
        if row.get("exch_seg") == exch_seg and str(row.get("name", "")).upper() in wanted:
            return row
    return None


_ltp_cache = {}  # underlying -> (fetched_at_epoch_seconds, {"ltp":..., "change_pct":...})
_LTP_CACHE_TTL_SECONDS = 5  # short-lived: keeps a page load's several calls to the
# same index from each hitting Angel One separately, without going stale for a trial


def get_index_ltp(underlying: str) -> dict:
    """Real spot LTP + change% for NIFTY / BANKNIFTY / SENSEX, using the
    hardcoded token constants above - no scrip-master download needed.
    Cached for a few seconds so one page load (which can call this for the
    same underlying from several endpoints - overview, nifty-zone, chain)
    doesn't fire off several redundant real HTTP round-trips."""
    cached = _ltp_cache.get(underlying)
    if cached and (time.time() - cached[0]) < _LTP_CACHE_TTL_SECONDS:
        return cached[1]
    _ensure_session()
    if underlying in _INDEX_TOKENS:
        exch_seg, tradingsymbol, symboltoken = _INDEX_TOKENS[underlying]
    else:
        raise RuntimeError(f"No known token for {underlying}.")
    resp = requests.post(
        BASE_URL + "/rest/secure/angelbroking/order/v1/getLtpData",
        json={"exchange": exch_seg, "tradingsymbol": tradingsymbol, "symboltoken": symboltoken},
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
    change_abs = round(ltp - close, 2)
    result = {"ltp": ltp, "change_pct": change_pct, "change_abs": change_abs}
    _ltp_cache[underlying] = (time.time(), result)
    return result


def find_equity_token(symbol: str) -> dict | None:
    """Looks up the plain NSE equity row for a stock symbol (e.g. 'RELIANCE')
    - not its futures/options rows, which share the same 'name' field in the
    scrip master but have a different instrumenttype. Equity rows have an
    empty instrumenttype and a '-EQ' suffixed tradingsymbol (e.g.
    'RELIANCE-EQ'), which is what distinguishes them here."""
    rows = _load_scrip_master()
    wanted = symbol.upper()
    for row in rows:
        if (row.get("exch_seg") == "NSE" and str(row.get("name", "")).upper() == wanted
                and not row.get("instrumenttype")):
            return row
    return None


def get_stock_ltp(symbol: str) -> dict:
    """Real spot LTP + change% for an individual F&O stock (e.g. RELIANCE).
    Unlike the 3 major indices, stock tokens aren't few enough to hardcode,
    so this looks the token up in the (disk-cached) scrip master. Cached in
    memory for a few seconds for the same reason as get_index_ltp."""
    cached = _ltp_cache.get("STOCK:" + symbol)
    if cached and (time.time() - cached[0]) < _LTP_CACHE_TTL_SECONDS:
        return cached[1]
    _ensure_session()
    row = find_equity_token(symbol)
    if row is None:
        raise RuntimeError(f"Could not find an NSE equity token for {symbol} in the scrip master.")
    resp = requests.post(
        BASE_URL + "/rest/secure/angelbroking/order/v1/getLtpData",
        json={"exchange": "NSE", "tradingsymbol": row["symbol"], "symboltoken": row["token"]},
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
    result = {"ltp": ltp, "change_pct": change_pct, "change_abs": round(ltp - close, 2)}
    _ltp_cache["STOCK:" + symbol] = (time.time(), result)
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
