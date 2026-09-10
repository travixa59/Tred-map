"""
AI TRADE REASONING (LLM layer on top of the existing rule-based engine).

This is the ONLY file that talks to an LLM API. It does NOT pick the
strike or replace probability.py / the OI-first selection logic in
main.py - that stays deterministic and auditable. This module's only job
is to take the numbers those layers already produced (spot, indicators,
OI bias, the shortlisted CE/PE, mock/real news headlines) and have an LLM
write a plain-English verdict: does the setup actually look tradeable,
what's the main risk, and a confidence label - the same way a second pair
of eyes would sanity-check a trade before you take it.

--------------------------------------------------------------------------
TWO PROVIDERS - pick whichever env var you set:
--------------------------------------------------------------------------
- GEMINI_API_KEY    -> Google Gemini (Flash models have a genuinely free,
                        ongoing tier - no card, no trial expiry - the
                        right choice while you're just trying this out).
                        Get one at https://aistudio.google.com/apikey
                        pip install google-genai
- ANTHROPIC_API_KEY -> Claude (small one-time trial credit, then paid -
                        switch to this later if Gemini's free-tier rate
                        limits (a handful of requests/minute, capped
                        requests/day) become the bottleneck).
                        pip install anthropic

If BOTH are set, Gemini is preferred (it's the free one) unless
AI_PROVIDER=anthropic is set explicitly. If NEITHER is set,
is_configured() is False and main.py's /aitrade/ai-analysis endpoint
returns a clear "not configured" message instead of crashing - same
fallback pattern as angel_one_client.py.

--------------------------------------------------------------------------
KNOWN GAPS - next steps:
--------------------------------------------------------------------------
- News input is mock_data.generate_mock_news() for now (clearly labeled
  is_mock: true) - swap in a real news/sentiment API later; this module
  doesn't care where the headlines came from.
- No response caching yet - each call to /aitrade/ai-analysis is a fresh
  LLM call. Fine for a single trader checking a handful of times a day;
  add a short TTL cache (same pattern as angel_one_client's _ltp_cache)
  if this gets hit a lot more often than that, especially on Gemini's
  free tier where the daily request cap is the thing that runs out first.
- The model is asked to return strict JSON; if it ever returns something
  that doesn't parse, this raises and main.py falls back gracefully
  rather than showing garbage to the user.
"""

import os
import json

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-flash-latest"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# Explicit override if someone has both keys set and wants to force one.
AI_PROVIDER = os.environ.get("AI_PROVIDER", "").strip().lower()

_client = None  # holds whichever SDK client got initialized (Gemini or Anthropic)


def _active_provider() -> str | None:
    """Which provider actually gets used. Gemini is preferred by default
    because its Flash tier is genuinely free for a trial - Anthropic only
    if explicitly requested via AI_PROVIDER, or if it's the only key set."""
    if AI_PROVIDER == "gemini" and GEMINI_API_KEY:
        return "gemini"
    if AI_PROVIDER == "anthropic" and ANTHROPIC_API_KEY:
        return "anthropic"
    if GEMINI_API_KEY:
        return "gemini"
    if ANTHROPIC_API_KEY:
        return "anthropic"
    return None


def is_configured() -> bool:
    return _active_provider() is not None


def _get_client(provider: str):
    global _client
    if _client is not None:
        return _client
    if provider == "gemini":
        from google import genai  # imported lazily so the app still boots without the package installed
        _client = genai.Client(api_key=GEMINI_API_KEY)
    else:
        import anthropic  # imported lazily so the app still boots without the package installed
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


_SYSTEM_PROMPT = (
    "You are a cautious, experienced options trading assistant reviewing a "
    "trade setup that a separate rule-based engine already produced from "
    "real option-chain OI data and technical indicators. You do NOT pick "
    "the strike or override that engine - your job is to sanity-check its "
    "pick in plain English: does the story actually hang together (OI bias, "
    "indicators, and news all pointing the same way, or are they in "
    "conflict), what is the single biggest risk to this trade right now, "
    "and how confident should a retail trader be. Be direct about "
    "conflicting signals and weak setups - do not talk every setup up. "
    "Reply with ONLY a JSON object, no markdown fences, no extra text, "
    "matching exactly this shape: "
    '{"verdict": "TAKE" | "SKIP" | "WAIT_FOR_CONFIRMATION", '
    '"confidence": "HIGH" | "MEDIUM" | "LOW", '
    '"summary": "<one or two sentence plain-English take>", '
    '"key_risk": "<the single biggest risk to this specific trade>", '
    '"supporting_points": ["<short bullet>", "..."], '
    '"conflicting_points": ["<short bullet>", "..."]}'
)


def _build_user_prompt(context: dict) -> str:
    return (
        "UNDERLYING: " + context["underlying"] + "\n"
        "SPOT: " + str(context["spot"]) + "\n"
        "UNDERLYING DIRECTION PROBABILITY (bullish %): " + str(context["underlying_probability"]) + "\n"
        "INDICATOR REASONS: " + json.dumps(context["underlying_reasons"]) + "\n"
        "CHAIN-WIDE OI BIAS: " + context["overall_oi_bias"] + " (" + context["overall_final_signal"] + ")\n"
        "RULE-ENGINE'S SHORTLISTED CALL SETUP: " + json.dumps(context["best_ce"]) + "\n"
        "RULE-ENGINE'S SHORTLISTED PUT SETUP: " + json.dumps(context["best_pe"]) + "\n"
        "RECENT NEWS HEADLINES (mock=demo data if is_mock is true): " + json.dumps(context["news"]) + "\n\n"
        "Review the side (CE or PE) that the chain-wide OI bias and final "
        "signal actually favour, using the indicator reasons and news as "
        "supporting or conflicting evidence. Return the JSON object only."
    )


def _clean_json_text(raw_text: str) -> str:
    raw_text = raw_text.strip()
    # Defensive: strip accidental markdown fences even though the prompt
    # explicitly asks the model not to include them.
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.lower().startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()
    return raw_text


def _call_gemini(client, prompt: str) -> str:
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config={
            "system_instruction": _SYSTEM_PROMPT,
            "response_mime_type": "application/json",
            "max_output_tokens": 800,
        },
    )
    return resp.text


def _call_anthropic(client, prompt: str) -> str:
    resp = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=800,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")


def generate_trade_analysis(context: dict) -> dict:
    """context must contain: underlying, spot, underlying_probability,
    underlying_reasons, overall_oi_bias, overall_final_signal, best_ce,
    best_pe, news. Returns the parsed verdict dict (see _SYSTEM_PROMPT for
    shape), plus a 'provider' key noting which LLM answered. Raises on any
    API or parsing failure - main.py catches this and falls back to the
    rule-based-only view."""
    provider = _active_provider()
    if provider is None:
        raise RuntimeError(
            "No AI provider is configured. Set GEMINI_API_KEY (free tier - "
            "get one at https://aistudio.google.com/apikey) or "
            "ANTHROPIC_API_KEY as an environment variable first."
        )
    client = _get_client(provider)
    prompt = _build_user_prompt(context)
    raw_text = _call_gemini(client, prompt) if provider == "gemini" else _call_anthropic(client, prompt)
    parsed = json.loads(_clean_json_text(raw_text))
    parsed["provider"] = provider
    return parsed
