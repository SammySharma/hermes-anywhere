#!/usr/bin/env python3
"""The Alpha Desk setups as pure functions, shared by the live scan and the backtester so both trade the same rules.

Three sleeves share one risk budget:
- ORB_PLAY: opening-range breakout on stocks in play (below). The momentum sleeve, for days that open one-sided.
- ORB_NEWS: the same order on a stock whose overnight news the agent scored in the order's direction before the open.
  It needs less opening volume. This sleeve cannot be backtested and is judged on forward paper trades only.
- VWAP_FADE: fade a stock stretched far from its session VWAP on ordinary volume, back to VWAP. The reversal sleeve,
  only on days that open two-sided, when breakouts tend to fail.
`day_mode` decides which sleeves run. It is computed from the opening candles of the shortlist, never by the agent.

ORB setup: opening-range breakout on stocks in play (Zarattini, Barbon and Aziz 2024, adapted to NSE costs).
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
    # news sleeve
    "min_or_rvol_news": 1.5,  # opening volume needed when the agent scored the overnight news in the order's direction
    # day mode
    "trend_breadth": 0.4,   # |up - down| / (up + down) of the shortlist's opening candles at or above this: trend day
    "range_risk_mult": 0.5, # ORB orders risk this share of the normal budget on a range day
    # reversal sleeve (range days only)
    "fade_times": ["11:30", "12:30", "13:30"],  # the only moments a fade is evaluated, live and in the backtest
    "fade_stretch_atr": 0.8,  # distance from session VWAP, as a share of the daily ATR, that counts as stretched
    "fade_stop_atr": 0.4,     # stop beyond the entry, as a share of the daily ATR. The target is VWAP
    "fade_max_or_rvol": 1.5,  # at or above this opening volume the stock is in play: its move has a reason, do not fade
    "fade_top": 2,            # fades proposed per check at most, most stretched first
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
    floor = min(p["min_or_rvol"], p["min_or_rvol_news"])  # news_ok applies the stricter bar when there is no news
    if rvol < floor:
        return None, f"or_rvol {rvol:.2f} below {floor}"
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


def day_mode(ups, downs, p=PARAMS):
    """(mode, breadth). A one-sided open is a trend day: breakouts get full risk and nothing is faded. A two-sided open
    is a range day: breakouts get reduced risk and the reversal sleeve is switched on."""
    n = ups + downs
    if not n:
        return "range", 0.0
    breadth = (ups - downs) / n
    return ("trend" if abs(breadth) >= p["trend_breadth"] else "range"), round(breadth, 2)


def orb_risk_mult(mode, p=PARAMS):
    return 1.0 if mode == "trend" else p["range_risk_mult"]


def news_ok(plan, score, p=PARAMS):
    """(tradable, setup) for an ORB plan given the agent's pre-open news score (+1, 0, -1 or None).
    News against the order blocks it. News with it lowers the volume bar. No news needs a stock in play."""
    want = 1 if plan["side"] == "LONG" else -1
    if score and score == -want:
        return False, plan["setup"]
    if score == want:
        return True, "ORB_NEWS"
    return plan["or_rvol"] >= p["min_or_rvol"], plan["setup"]


def fade_plan(ltp, vwap, atr, or_rvol, p=PARAMS):
    """A fade back to VWAP if price is stretched and the stock was not in play at the open. (plan, None) or (None, why)."""
    if not atr or atr <= 0 or not vwap:
        return None, "no ATR or VWAP"
    if or_rvol is None or or_rvol >= p["fade_max_or_rvol"]:
        return None, "in play at the open, or no volume history"
    stretch = (ltp - vwap) / atr
    if abs(stretch) < p["fade_stretch_atr"]:
        return None, f"only {stretch:+.2f} ATR from VWAP"
    short = stretch > 0
    stop = _tick(ltp + p["fade_stop_atr"] * atr, True) if short else _tick(ltp - p["fade_stop_atr"] * atr, False)
    return {"side": "SHORT" if short else "LONG", "entry_ref": round(ltp, 2), "stop": stop,
            "target": _tick(vwap, True) if short else _tick(vwap, False), "stretch_atr": round(stretch, 2),
            "or_rvol": round(or_rvol, 2), "setup": "VWAP_FADE"}, None


def fade_minutes(p=PARAMS):
    return [int(t[:2]) * 60 + int(t[3:]) for t in p["fade_times"]]
