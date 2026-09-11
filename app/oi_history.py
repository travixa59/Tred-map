"""
Real multi-timeframe OI bias history.

The reference dashboard's OI bias strip (OI / LATEST / 3M / 5M / 15M / 30M)
is supposed to show whether the chain-wide OI bias 3/5/15/30 minutes ago
agreed with the current bias - a real confirmation signal. Angel One's
option-quote endpoint only gives a live snapshot, not historical intraday
OI, so there is no live data source for "what was the bias 15 minutes
ago" unless *we* keep recording it ourselves.

This module is that recorder: a small in-process, in-memory time series
of (timestamp, overall_bias) samples per (underlying, expiry), built up
from real live snapshots every time the option chain is polled in live
mode. Once ~30 minutes of samples have accumulated it gives a genuine
multi-timeframe confirmation; before that, timeframes with no data yet
are returned as None (never guessed/faked) so the frontend can show
"Building..." instead of a random label.

LIMITATION: this is per-process memory, not a database - history resets
on server restart/redeploy, and does not sync across multiple worker
processes if the app is ever run with more than one. That's an accepted
tradeoff for now (no extra infra needed); if the deployment moves to
multiple workers, this should move to a shared store (Redis/DB table)
instead.
"""

import time
from collections import defaultdict

# Keep a little more than the longest window (30M) so "30 minutes ago"
# always has a sample available once the server's been up that long.
_MAX_AGE_SECONDS = 35 * 60

_WINDOWS = {"3M": 3 * 60, "5M": 5 * 60, "15M": 15 * 60, "30M": 30 * 60}

# key -> list[(timestamp, bias)], oldest first.
_history: dict[tuple, list] = defaultdict(list)


def record(underlying: str, expiry: str, overall_bias: str) -> None:
    """Append a real observed bias sample and trim anything older than
    we'll ever need. Call this once per live chain refresh, right after
    overall_bias is computed from real OI."""
    if not overall_bias:
        return
    key = (underlying, expiry)
    now = time.time()
    bucket = _history[key]
    bucket.append((now, overall_bias))
    cutoff = now - _MAX_AGE_SECONDS
    while bucket and bucket[0][0] < cutoff:
        bucket.pop(0)


def _bias_at_or_before(bucket: list, target_time: float) -> str | None:
    """Latest recorded bias at or before target_time. None if we don't
    have anything that far back yet (e.g. server just started)."""
    result = None
    for ts, bias in bucket:
        if ts <= target_time:
            result = bias
        else:
            break
    return result


def timeframe_bias(underlying: str, expiry: str, overall_bias: str) -> dict:
    """Real OI/LATEST/3M/5M/15M/30M strip. OI and LATEST are always the
    current bias (the CURRENT reading, not historical). Each of
    3M/5M/15M/30M is the ACTUAL bias recorded that many minutes ago, or
    None if we don't have that much history yet - the frontend shows
    those as 'Building...' rather than making something up."""
    key = (underlying, expiry)
    bucket = _history.get(key, [])
    now = time.time()
    result = {"OI": overall_bias, "LATEST": overall_bias}
    for label, seconds_ago in _WINDOWS.items():
        result[label] = _bias_at_or_before(bucket, now - seconds_ago)
    return result


def has_history(underlying: str, expiry: str) -> bool:
    return bool(_history.get((underlying, expiry)))
