#!/usr/bin/env python3
"""The Alpha Desk v3 setup as pure functions, shared by the live scan and the backtester so both trade the same rules.

Setup: opening-range breakout on stocks in play (Zarattini, Barbon and Aziz 2024, adapted to NSE costs).
- A stock is in play when its opening-range volume is a multiple of its own average for that window.
- The side is the direction of the opening-range candle, so the book is long and short on its own evidence and does
  not depend on the index trending up or down.
- Entry is a stop order at the opening-range extreme, known at 09:30, so nothing has to be chased.
- The stop is a fraction of the daily ATR, so it scales with each stock's noise instead of a fixed percent.
- There is no profit target and no move to breakeven: a trade ends at its stop or at the square-off.

Every number here is a hypothesis until `backtest.py` supports it on NSE data. Change them only from a backtest.
"""

import math
from datetime import datetime, time, timedelta

OPEN = time(9, 15)
TICK = 0.05

PARAMS = {
    "or_minutes": 15,       # opening range: 09:15 to 09:30
    "min_or_rvol": 2.0,     # opening-range volume / its average over the prior sessions
    "rvol_days": 14,        # sessions in that average
    "min_rvol_days": 5,     # fewer prior sessions than this: no opinion, no trade
    "stop_atr": 0.4,        # stop distance as a fraction of the 14-day daily ATR
    "max_or_atr": 1.0,      # skip when the opening range already spans this much of the ATR (move is spent)
    "buf_pct": 0.02,        # trigger this far beyond the opening-range extreme, in percent
    "entry_until": "12:00", # an untriggered order is cancelled at this time
    "top": 6,               # orders armed per day at most, ranked by opening-range relative volume
    "extended_r": 0.25,     # never arm or chase once price is this far past the trigger, in units of the stop distance
}


def or_end(p=PARAMS):
    return (datetime.combine(datetime(2000, 1, 3), OPEN) + timedelta(minutes=p["or_minutes"])).time()


def until(p=PARAMS):
    h, m = p["entry_until"].split(":")
    return time(int(h), int(m))


def _tick(px, up):
    n = px / TICK
    return round((math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)) * TICK, 2)


def opening_plan(o, h, l, c, vol, prior_vols, atr, p=PARAMS):
    """Today's order from the opening range (open, high, low, close, volume), the same window's volume on prior
    sessions (oldest first) and the daily ATR in rupees. Returns (plan, None) or (None, reason)."""
    prior = [v for v in prior_vols if v and v > 0][-p["rvol_days"]:]
    if len(prior) < p["min_rvol_days"]:
        return None, "too little volume history"
    if not atr or atr <= 0 or not vol:
        return None, "no ATR or volume"
    rvol = vol / (sum(prior) / len(prior))
    if rvol < p["min_or_rvol"]:
        return None, f"or_rvol {rvol:.2f} below {p['min_or_rvol']}"
    if c == o:
        return None, "flat opening range"
    rng = (h - l) / atr
    if rng > p["max_or_atr"]:
        return None, f"opening range is {rng:.2f} ATR"
    long_ = c > o
    trigger = _tick(h * (1 + p["buf_pct"] / 100), True) if long_ else _tick(l * (1 - p["buf_pct"] / 100), False)
    dist = p["stop_atr"] * atr
    stop = _tick(trigger - dist, False) if long_ else _tick(trigger + dist, True)
    return {"side": "LONG" if long_ else "SHORT", "trigger": trigger, "stop": stop, "or_rvol": round(rvol, 2),
            "or_high": round(h, 2), "or_low": round(l, 2), "or_range_atr": round(rng, 2),
            "stop_pct": round(abs(trigger - stop) / trigger * 100, 2), "setup": "ORB_PLAY"}, None
