#!/usr/bin/env python3
"""Paper-trading book for the Alpha Desk NSE intraday (MIS) desk: sizing, entries, exits, stop/target monitor."""

import argparse
import json
import os
import time as _time
from contextlib import contextmanager
from datetime import datetime, timedelta

import strategy
from market_data import (DATA_DIR, ENTRY_START, IST, LAST_ENTRY, OPEN, SQUARE_OFF, day_mode, fades, get_bars, get_quotes,
                         now_ist, scan, segment_of, shortable)

PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
DECISIONS_FILE = os.path.join(DATA_DIR, "decisions_log.json")
LOCK_FILE = os.path.join(DATA_DIR, ".lock")
NEWS_FILE = os.path.join(DATA_DIR, "news_scores.json")

# Hard rules. They live here, not in markdown, so the self-improvement loop cannot loosen them.
CAPITAL = 100000
LEVERAGE = 5  # MIS multiplier cap
RISK_PCT = 0.5  # max loss at the stop incl. fees and slippage, % of equity. Three open positions risk 1.5% together
STOP_MAX_PCT = 2.5  # a stop farther than this is not an intraday trade
MAX_COST_R = 0.25  # round-trip fees and slippage as a share of the planned loss. Tighter stops only feed the broker
MIN_RR = 1.5  # reward:risk after fees and slippage, checked only when a trade has a target
MAX_POSITIONS = 3
MAX_PENDING = 6  # armed stop-entry orders
MAX_TRADES_PER_DAY = 5
DAILY_LOSS_PCT = 1.5  # three full stops
# Slippage per fill (against us) and default MIS multiplier by Nifty 500 segment. Entries are stop orders into a
# breakout, which slip more than a passive fill. Pass --mis-multiplier with your broker's figure for a stock.
SEGMENTS = {"LARGE": {"slip": 0.05, "mult": 5}, "MID": {"slip": 0.08, "mult": 3}, "SMALL": {"slip": 0.12, "mult": 2}}


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written book


@contextmanager
def lock(timeout=45):
    """One writer at a time: the 15-minute scan, the monitor and an approved entry can overlap."""
    os.makedirs(DATA_DIR, exist_ok=True)
    start = _time.time()
    while True:
        try:
            os.close(os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if _time.time() - os.path.getmtime(LOCK_FILE) > 120:  # holder crashed
                    os.remove(LOCK_FILE)
                    continue
            except FileNotFoundError:
                continue
            if _time.time() - start > timeout:
                raise TimeoutError("Portfolio is locked by another command; retry shortly.")
            _time.sleep(0.2)
    try:
        yield
    finally:
        try:
            os.remove(LOCK_FILE)
        except FileNotFoundError:
            pass


def log_decision(entry):
    log = load_json(DECISIONS_FILE, [])
    log.append(entry)
    save_json(DECISIONS_FILE, log)


def load_portfolio(now):
    pf = load_json(PORTFOLIO_FILE, None)
    if pf is None:
        pf = {"portfolio_name": "Alpha Desk Intraday", "base_currency": "INR", "created_at": now.strftime("%Y-%m-%d"),
              "initial_capital": CAPITAL, "cash": CAPITAL, "positions": [], "pending": [], "trades": [],
              "day": {"date": "", "start_equity": CAPITAL, "trades_opened": 0, "halted": False},
              "strategy_version": 1}
    elif "positions" not in pf or pf.get("base_currency") != "INR":
        raise ValueError(f"{PORTFOLIO_FILE} is a legacy swing portfolio. Rename it (e.g. portfolio_v1.json) to start the intraday book.")
    today = now.date().isoformat()
    pf.setdefault("pending", [])
    if pf["day"]["date"] != today:
        pf["day"] = {"date": today, "start_equity": round(pf["cash"], 2), "trades_opened": 0, "halted": False}
        pf["pending"] = []  # yesterday's orders never carry over
    return pf


def norm(symbol):
    return symbol.upper().removesuffix(".NS")


# --- costs -----------------------------------------------------------------------------------------------------------

def charges(order_side, value):
    """Approximate NSE intraday equity charges for one order (verify against your broker's current rates)."""
    brokerage = min(20.0, 0.0003 * value)
    txn, sebi = 0.0000297 * value, 0.000001 * value
    gst = 0.18 * (brokerage + txn + sebi)
    statutory = 0.00025 * value if order_side == "SELL" else 0.00003 * value  # STT on sell, stamp duty on buy
    return brokerage + txn + sebi + gst + statutory


def sides(side):
    """(entry order, exit order)"""
    return ("BUY", "SELL") if side == "LONG" else ("SELL", "BUY")


def fill(order_side, px, seg="LARGE"):
    """Price after slippage, always against us."""
    slip = SEGMENTS[seg]["slip"] / 100
    return px * (1 + slip) if order_side == "BUY" else px * (1 - slip)


# --- sizing and rules ------------------------------------------------------------------------------------------------

def plan(side, price, stop, target, equity, margin_used, mult, seg="LARGE", risk_mult=1.0):
    """Size a trade and check the price rules. Returns the numbers plus a list of problems (empty = tradable).
    `risk_mult` can only shrink the risk budget (ORB orders on a range day)."""
    e, x = sides(side)
    sign = 1 if side == "LONG" else -1
    entry = fill(e, price, seg)
    risk_ps = (entry - stop) * sign
    if risk_ps <= 0 or (target is not None and (target - entry) * sign <= 0):
        above, below = ("above", "below") if sign > 0 else ("below", "above")
        return {"problems": [f"For a {side}, the stop must be {below} and the target {above} the entry"]}
    problems = []
    stop_pct = risk_ps / entry * 100
    if stop_pct > STOP_MAX_PCT:
        problems.append(f"Stop is {stop_pct:.2f}% from entry; the maximum is {STOP_MAX_PCT}%")
    budget = equity * RISK_PCT / 100 * max(0.1, min(1.0, risk_mult))
    cap = min(equity * LEVERAGE / MAX_POSITIONS, (equity - margin_used) * mult)

    def net_at(px, q):
        out = fill(x, px, seg)
        return (out - entry) * q * sign - charges(e, q * entry) - charges(x, q * out)

    qty = int(min(budget // risk_ps, cap // entry))
    while qty > 0 and -net_at(stop, qty) > budget:
        qty -= 1 + qty // 50
    if qty < 1:
        return {"problems": problems + ["Risk budget or free margin is too small for this stop"]}
    risk = -net_at(stop, qty)
    cost_r = 1 - (price - stop) * sign * qty / risk  # share of the planned loss that is fees and slippage
    if cost_r > MAX_COST_R:
        problems.append(f"Costs are {cost_r:.0%} of the risk at this stop (maximum {MAX_COST_R:.0%}); the stop is too tight")
    out = {"entry": round(entry, 2), "qty": qty, "notional": round(qty * entry, 2), "margin": round(qty * entry / mult, 2),
           "stop_pct": round(stop_pct, 2), "risk": round(risk, 2), "cost_r": round(cost_r, 2), "problems": problems}
    if target is not None:
        reward = net_at(target, qty)
        if reward / risk < MIN_RR:
            problems.append(f"Net reward:risk is {reward / risk:.2f}; it must be at least {MIN_RR}")
        out.update(reward=round(reward, 2), net_rr=round(reward / risk, 2))
    return out


def _margin_used(pf):
    return sum(p["qty"] * p["entry"] / p["mult"] for p in pf["positions"])


def size(symbol, side, price, stop, target=None, mult=None, now=None):
    now = now or now_ist()
    pf = load_portfolio(now)
    seg = segment_of(symbol)
    pl = plan(side.upper(), price, stop, target, pf["cash"], _margin_used(pf), min(mult or SEGMENTS[seg]["mult"], LEVERAGE), seg)
    return {"symbol": norm(symbol), "segment": seg, "side": side.upper(), "stop": stop, "target": target, **pl,
            "ok": not pl["problems"]}


# --- entries and exits -----------------------------------------------------------------------------------------------

def _risk_mult(setup):
    """Risk scale for a setup today. ORB orders shrink on a range day; the mode comes from the morning scan, not the caller."""
    mode = day_mode()
    return mode["orb_risk_mult"] if mode and setup.startswith("ORB") else 1.0


def _admit(pf, now, symbol, side, pending=False, setup=""):
    """Book-level reasons a new entry (or a new armed order) is not allowed right now."""
    d, problems = pf["day"], []
    if setup == "VWAP_FADE":
        mode = day_mode()
        if not mode or mode["mode"] != "range":
            problems.append("Fades are allowed only on a range day (run dayplan first; today is "
                            f"{mode['mode'] if mode else 'unclassified'})")
    if d["halted"] or pf["cash"] - d["start_equity"] <= -DAILY_LOSS_PCT / 100 * d["start_equity"]:
        problems.append("Daily loss limit reached; no new entries today")
    if d["trades_opened"] >= MAX_TRADES_PER_DAY:
        problems.append(f"Already took {MAX_TRADES_PER_DAY} trades today")
    if not pending and len(pf["positions"]) >= MAX_POSITIONS:
        problems.append(f"Already holding {MAX_POSITIONS} positions")
    if any(p["symbol"] == symbol for p in pf["positions"]):
        problems.append(f"{symbol} is already open; no averaging")
    if any(t["symbol"] == symbol and t["opened_at"][:10] == d["date"] for t in pf["trades"]):
        problems.append(f"{symbol} was already traded today; one trade per symbol per day")
    if side == "SHORT" and not shortable(symbol):
        problems.append(f"{symbol} is not in F&O: a short can lock at the upper circuit and go to auction")
    return problems


def _enter(pf, now, side, symbol, price, stop, target=None, qty=None, setup="", thesis="", sector="",
           confidence="medium", mult=None):
    """Open a position at `price` (before slippage) if every rule allows it. Returns (result, problems)."""
    problems = _admit(pf, now, symbol, side, setup=setup)
    seg = segment_of(symbol)
    mult = min(mult or SEGMENTS[seg]["mult"], LEVERAGE)
    pl = plan(side, price, stop, target, pf["cash"], _margin_used(pf), mult, seg, _risk_mult(setup))
    problems += pl["problems"]
    if qty and pl.get("qty") and qty > pl["qty"]:
        problems.append(f"Qty {qty} exceeds the maximum {pl['qty']} for this stop")
    if problems:
        return None, problems
    qty = qty or pl["qty"]
    e, _ = sides(side)
    pos = {"symbol": symbol, "side": side, "qty": qty, "entry": pl["entry"], "stop": stop, "stop0": stop,
           "target": target, "risk": round(pl["risk"] * qty / pl["qty"], 2),  # planned loss at the stop incl. costs: 1R
           "entry_fees": round(charges(e, qty * pl["entry"]), 2), "mult": mult, "segment": seg, "setup": setup,
           "thesis": thesis, "sector": sector, "confidence": confidence, "opened_at": _ts(now), "checked_to": _ts(now)}
    pf["positions"].append(pos)
    pf["day"]["trades_opened"] += 1
    log_decision({"type": "trade", "action": "ENTER", "time": _ts(now), "symbol": symbol, "side": side, "qty": qty,
                  "entry": pos["entry"], "stop": stop, "target": target, "setup": setup, "thesis": thesis,
                  "confidence": confidence})
    return {"status": "opened", "symbol": symbol, "segment": seg, "side": side, "qty": qty, "entry": pos["entry"],
            "stop": stop, "target": target, "risk": pos["risk"], "cost_r": pl["cost_r"], "net_rr": pl.get("net_rr"),
            "margin_used": round(_margin_used(pf), 2)}, []


def open_position(side, symbol, price, stop, target=None, qty=None, setup="", thesis="", sector="", confidence="medium",
                  mult=None, now=None):
    """Enter at the market now (a manual trade). The planned ORB entries go through `arm` instead."""
    now = now or now_ist()
    symbol = norm(symbol)
    with lock():
        pf = load_portfolio(now)
        if not ENTRY_START <= now.time() <= LAST_ENTRY:
            return {"error": "Outside the entry window (09:30-14:30 IST)"}
        res, problems = _enter(pf, now, side, symbol, price, stop, target, qty, setup, thesis, sector, confidence, mult)
        if problems:
            return {"error": "; ".join(problems)}
        save_json(PORTFOLIO_FILE, pf)
        return res


def arm(side, symbol, trigger, stop, target=None, qty=None, setup="ORB_PLAY", thesis="", sector="", mult=None,
        until=None, now=None):
    """Arm a stop-entry order: the monitor enters when price trades through `trigger`, until `until` (HH:MM).
    The user approves the level once, in advance, so the entry needs no agent turn and no chase."""
    now = now or now_ist()
    symbol = norm(symbol)
    until = until or strategy.PARAMS["entry_until"]
    sign = 1 if side == "LONG" else -1
    with lock():
        pf = load_portfolio(now)
        problems = _admit(pf, now, symbol, side, pending=True, setup=setup)
        if not setup.startswith("ORB"):
            problems.append("Only ORB orders are armed. A fade is entered with long or short after approval")
        if not ENTRY_START <= now.time() <= LAST_ENTRY or now.strftime("%H:%M") >= until:
            problems.append(f"Outside the arming window (09:30 to {until} IST)")
        if any(o["symbol"] == symbol for o in pf["pending"]):
            problems.append(f"{symbol} already has an armed order; disarm it first")
        if len(pf["pending"]) >= MAX_PENDING:
            problems.append(f"Already {MAX_PENDING} armed orders")
        seg = segment_of(symbol)
        m = min(mult or SEGMENTS[seg]["mult"], LEVERAGE)
        pl = plan(side, trigger, stop, target, pf["cash"], _margin_used(pf), m, seg, _risk_mult(setup))
        problems += pl["problems"]
        q = (get_quotes([symbol]) or [{}])[0]
        if q.get("ltp") and q.get("live") and (q["ltp"] - trigger) * sign > strategy.PARAMS["extended_r"] * abs(trigger - stop):
            problems.append(f"{symbol} is already at {q['ltp']}, well past the trigger {trigger}; do not chase")
        if problems:
            return {"error": "; ".join(problems)}
        order = {"symbol": symbol, "side": side, "trigger": trigger, "stop": stop, "target": target, "qty": qty,
                 "mult": mult, "setup": setup, "thesis": thesis, "sector": sector, "until": until,
                 "armed_at": _ts(now), "checked_to": _ts(now)}
        pf["pending"].append(order)
        save_json(PORTFOLIO_FILE, pf)
        log_decision({"type": "order", "action": "ARM", "time": _ts(now), **{k: order[k] for k in
                      ("symbol", "side", "trigger", "stop", "setup", "thesis", "until")}})
        return {"status": "armed", "symbol": symbol, "segment": seg, "side": side, "trigger": trigger, "stop": stop,
                "until": until, "qty_now": min(qty or pl["qty"], pl["qty"]), "risk": pl["risk"], "cost_r": pl["cost_r"]}


def disarm(symbol, now=None):
    """Cancel the armed order in `symbol`, or every armed order with `all`."""
    now = now or now_ist()
    with lock():
        pf = load_portfolio(now)
        gone = [o["symbol"] for o in pf["pending"] if symbol.lower() == "all" or o["symbol"] == norm(symbol)]
        pf["pending"] = [o for o in pf["pending"] if o["symbol"] not in gone]
        save_json(PORTFOLIO_FILE, pf)
        for sym in gone:
            log_decision({"type": "order", "action": "DISARM", "time": _ts(now), "symbol": sym})
        return {"status": "disarmed", "symbols": gone}


def _triggered(order, bars):
    """First 1-minute bar that trades through the order's trigger -> (fill price before slippage, bar time), else None.
    A bar that opens beyond the trigger fills at its open, as a stop order would."""
    sgn = 1 if order["side"] == "LONG" else -1
    for ts, b in bars.iterrows():
        if ts.strftime("%H:%M") >= order["until"]:
            break
        if ((b["High"] if sgn > 0 else b["Low"]) - order["trigger"]) * sgn >= 0:
            return (b["Open"] if (b["Open"] - order["trigger"]) * sgn > 0 else order["trigger"]), ts
    return None


def _close(pf, pos, price, reason, qty, at, note=""):
    """Book an exit of `qty` (default all) at `price` before slippage. Updates cash and the position; returns the trade."""
    qty = min(qty or pos["qty"], pos["qty"])
    frac = qty / pos["qty"]
    _, x = sides(pos["side"])
    sign = 1 if pos["side"] == "LONG" else -1
    exit_fill = fill(x, price, pos["segment"])
    gross = (exit_fill - pos["entry"]) * qty * sign
    fees = pos["entry_fees"] * frac + charges(x, qty * exit_fill)
    net, risk = gross - fees, pos["risk"] * frac
    trade = {"symbol": pos["symbol"], "side": pos["side"], "qty": qty, "entry": pos["entry"], "exit": round(exit_fill, 2),
             "opened_at": pos["opened_at"], "closed_at": _ts(at),
             "hold_min": round((at - _parse(pos["opened_at"])).total_seconds() / 60, 1),
             "gross": round(gross, 2), "fees": round(fees, 2), "net": round(net, 2),
             "r": round(net / risk, 2) if risk else None, "setup": pos["setup"], "sector": pos["sector"],
             "segment": pos["segment"], "reason": reason, "stop0": pos["stop0"], "target": pos["target"], "note": note}
    pf["cash"] += net
    pf["trades"].append(trade)
    if qty >= pos["qty"]:
        pf["positions"].remove(pos)
    else:
        pos["qty"] -= qty
        pos["entry_fees"] -= pos["entry_fees"] * frac
        pos["risk"] -= risk
    log_decision({"type": "trade", "action": "EXIT", "time": _ts(at), "symbol": pos["symbol"], "side": pos["side"],
                  "qty": qty, "exit": trade["exit"], "reason": reason, "net": trade["net"], "r": trade["r"],
                  "setup": pos["setup"], "original_thesis": pos["thesis"], "note": note})
    return trade


def exit_position(symbol, price, reason, qty=None, note="", now=None):
    now = now or now_ist()
    symbol = norm(symbol)
    with lock():
        pf = load_portfolio(now)
        pos = next((p for p in pf["positions"] if p["symbol"] == symbol), None)
        if not pos:
            return {"error": f"No open position in {symbol}"}
        if qty and qty > pos["qty"]:
            return {"error": f"Only {pos['qty']} shares of {symbol} are open"}
        trade = _close(pf, pos, price, reason, qty, now, note)
        save_json(PORTFOLIO_FILE, pf)
        return {"status": "closed", **trade, "cash": round(pf["cash"], 2)}


def _walk(pos, bars):
    """Replay 1-minute bars. Returns (reason, price, time, stop): the first stop or target touch, else reason None.
    A bar that touches both counts as a stop. The stop is never moved to cost: that turned winners into scratches."""
    sgn = 1 if pos["side"] == "LONG" else -1
    stop = pos["stop"]
    for ts, b in bars.iterrows():
        adverse, favorable = (b["Low"], b["High"]) if sgn > 0 else (b["High"], b["Low"])
        if (adverse - stop) * sgn <= 0:
            px = stop if (b["Open"] - stop) * sgn > 0 else b["Open"]  # a gap through the stop fills at the open
            return ("stop" if stop == pos["stop0"] else "trail-stop"), px, ts, stop
        if pos.get("target") is not None and (favorable - pos["target"]) * sgn >= 0:
            return "target", pos["target"], ts, stop
    return None, None, None, stop


def _seen(new, now):
    """Time of the last completed 1-minute bar in `new`. The bar still forming is replayed again next minute, so a
    trigger or stop touched in its remaining seconds is not missed."""
    done = new[new.index + timedelta(minutes=1) <= now]
    return _ts(done.index[-1]) if not done.empty else None


def _ltp_exit(pos, px):
    """(reason, fill price) if the latest price is already through the stop or the target, else None."""
    sgn = 1 if pos["side"] == "LONG" else -1
    if (px - pos["stop"]) * sgn <= 0:
        return ("stop" if pos["stop"] == pos["stop0"] else "trail-stop"), px  # already past the stop: fills at the market
    if pos.get("target") is not None and (px - pos["target"]) * sgn >= 0:
        return "target", pos["target"]
    return None


def monitor(now=None):
    """Fill armed orders at their trigger, book stop exits, cancel unfilled orders at their cut-off, square off from
    15:15, and halt the day at the daily loss limit.
    The latest price comes from nse-live. Yahoo 1-minute bars replay what happened between polls, so a touch of a
    trigger or stop that has since reversed is not missed, and they stand in for the price when nse-live is down."""
    now = now or now_ist()
    with lock():
        pf = load_portfolio(now)
        d, exits, warn, last, entries = pf["day"], [], [], {}, []
        was_halted = d["halted"]
        syms = sorted({p["symbol"] for p in pf["positions"] + pf["pending"]})
        quotes = {q["symbol"]: q for q in get_quotes(syms)} if syms else {}
        bars = get_bars(syms, "1d", "1m", now) if syms else {}
        due = now.time() >= SQUARE_OFF
        for o in list(pf["pending"]):  # armed stop-entries first, so a fill is managed in this same pass
            b, q = bars.get(o["symbol"]), quotes.get(o["symbol"], {})
            sgn, hit = (1 if o["side"] == "LONG" else -1), None
            if b is not None and not b.empty:
                new = b[b.index > _parse(o["checked_to"])]
                hit = _triggered(o, new)
                o["checked_to"] = _seen(new, now) or o["checked_to"]
            elif q.get("live") and (q["ltp"] - o["trigger"]) * sgn >= 0 and now.strftime("%H:%M") < o["until"]:
                hit = (q["ltp"], now)  # no bars: the live price is already through the trigger, fill at the market
            if hit:
                pf["pending"].remove(o)
                res, problems = _enter(pf, hit[1], o["side"], o["symbol"], float(hit[0]), o["stop"], o["target"], o["qty"],
                                       o["setup"], o["thesis"], o["sector"], "medium", o["mult"])
                if res:
                    # replay the trigger bar too: if it also reached the stop, count the stop
                    pf["positions"][-1]["checked_to"] = _ts(hit[1].replace(second=0) - timedelta(minutes=1))
                    entries.append(res)
                else:
                    warn.append(f"{o['symbol']} triggered but was not entered: {'; '.join(problems)}")
                    log_decision({"type": "skip", "time": _ts(now), "symbol": o["symbol"], "setup": o["setup"],
                                  "reason": "triggered, blocked: " + "; ".join(problems)})
            elif now.strftime("%H:%M") >= o["until"] or due:
                pf["pending"].remove(o)
                log_decision({"type": "order", "action": "EXPIRE", "time": _ts(now), "symbol": o["symbol"]})
        for pos in list(pf["positions"]):
            sym, b, q = pos["symbol"], bars.get(pos["symbol"]), quotes.get(pos["symbol"], {})
            px = q["ltp"] if q.get("live") else None
            if b is not None and not b.empty:
                new = b[b.index > _parse(pos["checked_to"])]
                reason, fp, ts, pos["stop"] = _walk(pos, new)
                if reason:
                    exits.append(_close(pf, pos, fp, reason, None, ts))
                    continue
                pos["checked_to"] = _seen(new, now) or pos["checked_to"]
                px = px or float(b["Close"].iloc[-1])
            if px is None and due:
                px = q.get("ltp")  # a stale official price still beats leaving the position open
            if px is None:
                warn.append(f"No price for {sym}; position unmanaged" + (". SQUARE OFF MANUALLY" if due else ""))
                continue
            hit = _ltp_exit(pos, px)
            if hit:
                exits.append(_close(pf, pos, hit[1], hit[0], None, now))
                continue
            last[sym] = px
            if due or pos["opened_at"][:10] != d["date"]:
                exits.append(_close(pf, pos, px, "square-off", None, now))

        def unrealized(p):
            return (last[p["symbol"]] - p["entry"]) * p["qty"] * (1 if p["side"] == "LONG" else -1)

        day_pnl = pf["cash"] - d["start_equity"] + sum(unrealized(p) for p in pf["positions"] if p["symbol"] in last)
        if not d["halted"] and day_pnl <= -DAILY_LOSS_PCT / 100 * d["start_equity"]:
            d["halted"], pf["pending"] = True, []
            for pos in list(pf["positions"]):
                if pos["symbol"] in last:
                    exits.append(_close(pf, pos, last[pos["symbol"]], "daily-loss-halt", None, now))
            day_pnl = pf["cash"] - d["start_equity"]
        save_json(PORTFOLIO_FILE, pf)
        return {"time_ist": _ts(now), "entries": entries, "exits": exits, "halted": d["halted"],
                "pending": [{k: o[k] for k in ("symbol", "side", "trigger", "stop", "until")} for o in pf["pending"]],
                "halt_triggered": d["halted"] and not was_halted,
                "day_pnl": round(day_pnl, 2),
                "open": [{"symbol": p["symbol"], "side": p["side"], "qty": p["qty"], "entry": p["entry"],
                          "ltp": round(last[p["symbol"]], 2) if p["symbol"] in last else None, "stop": p["stop"],
                          "target": p["target"],
                          "unrealized": round(unrealized(p), 2) if p["symbol"] in last else None}
                         for p in pf["positions"]],
                "warnings": warn}


def _throttled(text, now, minutes=10):
    """True the first time `text` appears and again after `minutes`: a data outage would otherwise alert every minute."""
    path = os.path.join(DATA_DIR, ".monitor_warn.json")
    last = load_json(path, {})
    if last.get("text") == text and now.timestamp() - last.get("t", 0) < minutes * 60:
        return False
    save_json(path, {"text": text, "t": now.timestamp()})
    return True


def _brief(res, now):
    """Plain-text alert for a Hermes script-only cron job. Empty, so Hermes stays silent, unless a position closed or the
    day halted. A missing price alone is repeated at most every 10 minutes."""
    lines = [f"ENTER {e['symbol']} {e['side']} {e['qty']} @ {e['entry']} stop {e['stop']} (risk {e['risk']:.0f})"
             for e in res.get("entries", [])]
    lines += [f"EXIT {t['symbol']} {t['side']} {t['reason']}: {t['net']:+.0f}" + (f" ({t['r']:+.2f}R)" if t["r"] is not None else "")
              for t in res["exits"]]
    if res["halt_triggered"]:
        lines.append("DAILY LOSS LIMIT reached: positions closed, no new entries today")
    if lines:
        return "\n".join(lines + res["warnings"] + [f"Day P&L {res['day_pnl']:+.0f}"])
    warnings = "\n".join(res["warnings"])
    return warnings if warnings and _throttled(warnings, now) else ""


def note(symbol, text, now=None):
    """Attach a one-line post-mortem to the latest closed trade in `symbol`. The monitor closes stops and targets
    itself, so `exit --note` only ever covers manual exits."""
    now = now or now_ist()
    symbol = norm(symbol)
    with lock():
        pf = load_portfolio(now)
        trade = next((t for t in reversed(pf["trades"]) if t["symbol"] == symbol), None)
        if not trade:
            return {"error": f"No closed trade in {symbol}"}
        trade["note"] = f"{trade['note']}; {text}" if trade["note"] else text
        save_json(PORTFOLIO_FILE, pf)
        log_decision({"type": "postmortem", "time": _ts(now), "symbol": symbol, "closed_at": trade["closed_at"],
                      "reason": trade["reason"], "r": trade["r"], "note": text})
        return {"status": "noted", "symbol": symbol, "closed_at": trade["closed_at"], "note": trade["note"]}


# --- reports ---------------------------------------------------------------------------------------------------------

def show(now=None):
    pf = load_portfolio(now or now_ist())
    used = _margin_used(pf)
    return {"name": pf["portfolio_name"], "equity": round(pf["cash"], 2), "initial_capital": pf["initial_capital"],
            "buying_power": round(pf["cash"] * LEVERAGE, 2), "margin_used": round(used, 2),
            "free_margin": round(pf["cash"] - used, 2), "day": pf["day"], "positions": pf["positions"],
            "pending": pf["pending"],
            "closed_trades": len(pf["trades"]),
            "rules": {"risk_pct": RISK_PCT, "stop_max_pct": STOP_MAX_PCT, "max_cost_r": MAX_COST_R, "min_rr_if_target": MIN_RR,
                      "max_positions": MAX_POSITIONS, "max_trades_per_day": MAX_TRADES_PER_DAY,
                      "daily_loss_pct": DAILY_LOSS_PCT, "leverage": LEVERAGE, "segments": SEGMENTS}}


def value(now=None):
    """Equity with open positions marked to live prices (falls back to entry if there is no quote)."""
    pf = load_portfolio(now or now_ist())
    qs = {q["symbol"]: q for q in get_quotes([p["symbol"] for p in pf["positions"]])} if pf["positions"] else {}
    rows, unreal = [], 0.0
    for p in pf["positions"]:
        ltp = qs.get(p["symbol"], {}).get("ltp") or p["entry"]
        u = (ltp - p["entry"]) * p["qty"] * (1 if p["side"] == "LONG" else -1)
        unreal += u
        rows.append({"symbol": p["symbol"], "side": p["side"], "qty": p["qty"], "entry": p["entry"], "ltp": ltp,
                     "unrealized": round(u, 2), "r_now": round(u / p["risk"], 2) if p["risk"] else None})
    equity = pf["cash"] + unreal
    return {"cash": round(pf["cash"], 2), "unrealized": round(unreal, 2), "equity": round(equity, 2),
            "total_return": round(equity - pf["initial_capital"], 2),
            "return_pct": round((equity / pf["initial_capital"] - 1) * 100, 2),
            "day_pnl": round(equity - pf["day"]["start_equity"], 2), "positions": rows}


def _news_today(now):
    return load_json(NEWS_FILE, {}).get(now.date().isoformat(), {})


def news(symbol, score, reason, now=None):
    """Record the agent's reading of a stock's overnight news: +1 good, -1 bad, 0 neutral. Only before the open, so a
    score can never be written with the day's price action in view."""
    now = now or now_ist()
    if now.time() >= OPEN:
        return {"error": "News is scored before 09:15 IST. A score written after the open is hindsight and is refused."}
    if score not in (-1, 0, 1):
        return {"error": "Score must be -1, 0 or 1"}
    with lock():
        allnews = load_json(NEWS_FILE, {})
        day = allnews.setdefault(now.date().isoformat(), {})
        day[norm(symbol)] = {"score": score, "reason": reason, "time": _ts(now)}
        save_json(NEWS_FILE, {k: allnews[k] for k in sorted(allnews)[-30:]})
        log_decision({"type": "news", "time": _ts(now), "symbol": norm(symbol), "score": score, "reason": reason})
    return {"status": "scored", "symbol": norm(symbol), "score": score, "scored_today": len(day)}


def dayplan(top=None, now=None):
    """Today's ORB candidates from `market_data.scan`, each sized against the book. Levels come from code, never
    from the agent: it may veto a name, not move a trigger or a stop. Pre-open news scores decide which names below
    the in-play volume bar still qualify, and block any order against its own news."""
    now = now or now_ist()
    res = scan(top)
    if "error" in res:
        return res
    pf, scores, keep = load_portfolio(now), _news_today(now), []
    for c in res["candidates"]:
        n = scores.get(c["symbol"])
        tradable, c["setup"] = strategy.news_ok(c, n["score"] if n else None)
        c["news"] = n and {"score": n["score"], "reason": n["reason"]}
        if not tradable and not (n and n["score"]):
            continue  # ordinary volume and no news: not a candidate at all
        seg = c["segment"]
        pl = plan(c["side"], c["trigger"], c["stop"], None, pf["cash"], _margin_used(pf), SEGMENTS[seg]["mult"], seg,
                  res["orb_risk_mult"])
        problems = pl["problems"] + _admit(pf, now, c["symbol"], c["side"], pending=True)
        if not tradable:
            problems.append("the pre-open news score is against this order")
        if c["state"] == "extended":
            problems.append("already well past the trigger; do not chase")
        c.update(qty=pl.get("qty"), risk=pl.get("risk"), cost_r=pl.get("cost_r"), ok=not problems, problems=problems)
        keep.append(c)
    # news-backed orders first, then by opening volume
    keep.sort(key=lambda c: (c["setup"] != "ORB_NEWS", -c["or_rvol"]))
    res["candidates"] = keep[:top or strategy.PARAMS["top"]]
    res["armed"] = [o["symbol"] for o in pf["pending"]]
    res["open"] = [p["symbol"] for p in pf["positions"]]
    return res


def fadeplan(now=None):
    """Reversal candidates on a range day, each sized against the book. The agent's one job here is the news check:
    a stretched stock with news behind the move is vetoed, not faded."""
    now = now or now_ist()
    res = fades()
    if "error" in res:
        return res
    pf = load_portfolio(now)
    for c in res["candidates"]:
        seg = c["segment"]
        pl = plan(c["side"], c["ltp"], c["stop"], c["target"], pf["cash"], _margin_used(pf), SEGMENTS[seg]["mult"], seg)
        problems = pl["problems"] + _admit(pf, now, c["symbol"], c["side"], setup="VWAP_FADE")
        c.update(qty=pl.get("qty"), risk=pl.get("risk"), cost_r=pl.get("cost_r"), net_rr=pl.get("net_rr"),
                 ok=not problems, problems=problems)
    return res


def skip(symbol, reason, setup="", now=None):
    now = now or now_ist()
    with lock():
        log_decision({"type": "skip", "time": _ts(now), "symbol": norm(symbol), "setup": setup, "reason": reason})
    return {"status": "logged", "symbol": norm(symbol)}


def history(n=20):
    return load_json(PORTFOLIO_FILE, {"trades": []})["trades"][-n:]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("show", "value"):
        sub.add_parser(name)
    sub.add_parser("monitor").add_argument("--brief", action="store_true",
                                           help="print nothing unless a position closed or the day halted")
    sub.add_parser("dayplan", help="today's ORB candidates, sized").add_argument("--top", type=int)
    sub.add_parser("fadeplan", help="reversal candidates on a range day, sized")
    nw = sub.add_parser("news", help="score a stock's overnight news before the open")
    nw.add_argument("symbol")
    nw.add_argument("--score", type=int, choices=[-1, 0, 1], required=True)
    nw.add_argument("--reason", required=True)
    g = sub.add_parser("arm", help="arm a stop-entry order from the day plan")
    g.add_argument("symbol")
    g.add_argument("--side", choices=["long", "short"], required=True)
    g.add_argument("--trigger", type=float, required=True)
    g.add_argument("--stop", type=float, required=True)
    g.add_argument("--target", type=float)
    g.add_argument("--qty", type=int)
    g.add_argument("--until", help="HH:MM IST, default from strategy.py")
    g.add_argument("--mis-multiplier", type=float, dest="mult")
    g.add_argument("--setup", default="ORB_PLAY")
    g.add_argument("--thesis", default="")
    g.add_argument("--sector", default="")
    sub.add_parser("disarm").add_argument("symbol", help="a symbol, or all")
    for name in ("size", "long", "short"):
        s = sub.add_parser(name)
        s.add_argument("symbol")
        s.add_argument("price", type=float)
        s.add_argument("--stop", type=float, required=True)
        s.add_argument("--target", type=float, help="optional: the default exit is the stop or the square-off")
        s.add_argument("--mis-multiplier", type=float, dest="mult")
        if name == "size":
            s.add_argument("--side", choices=["long", "short"], default="long")
        else:
            s.add_argument("--qty", type=int, help="default: the maximum the rules allow")
            s.add_argument("--setup", default="")
            s.add_argument("--thesis", default="")
            s.add_argument("--sector", default="")
            s.add_argument("--confidence", choices=["high", "medium", "low"], default="medium")
    x = sub.add_parser("exit")
    x.add_argument("symbol")
    x.add_argument("price", type=float)
    x.add_argument("--reason", required=True)
    x.add_argument("--qty", type=int, help="partial exit")
    x.add_argument("--note", default="")
    k = sub.add_parser("skip")
    k.add_argument("symbol")
    k.add_argument("--reason", required=True)
    k.add_argument("--setup", default="")
    n = sub.add_parser("note")
    n.add_argument("symbol")
    n.add_argument("text", help="one-line post-mortem for the latest closed trade in the symbol")
    sub.add_parser("history").add_argument("--n", type=int, default=20)
    a = p.parse_args()
    brief = a.cmd == "monitor" and a.brief

    try:
        if a.cmd == "show":
            result = show()
        elif a.cmd == "value":
            result = value()
        elif a.cmd == "monitor":
            result = monitor()
        elif a.cmd == "dayplan":
            result = dayplan(a.top)
        elif a.cmd == "fadeplan":
            result = fadeplan()
        elif a.cmd == "news":
            result = news(a.symbol, a.score, a.reason)
        elif a.cmd == "arm":
            result = arm(a.side.upper(), a.symbol, a.trigger, a.stop, a.target, a.qty, a.setup, a.thesis, a.sector,
                         a.mult, a.until)
        elif a.cmd == "disarm":
            result = disarm(a.symbol)
        elif a.cmd == "note":
            result = note(a.symbol, a.text)
        elif a.cmd == "size":
            result = size(a.symbol, a.side, a.price, a.stop, a.target, a.mult)
        elif a.cmd in ("long", "short"):
            result = open_position(a.cmd.upper(), a.symbol, a.price, a.stop, a.target, a.qty, a.setup, a.thesis,
                                   a.sector, a.confidence, a.mult)
        elif a.cmd == "exit":
            result = exit_position(a.symbol, a.price, a.reason, a.qty, a.note)
        elif a.cmd == "skip":
            result = skip(a.symbol, a.reason, a.setup)
        else:
            result = history(a.n)
    except (ValueError, TimeoutError) as e:
        if brief:  # a busy book just skips this minute; anything else must not fail silently
            if isinstance(e, ValueError):
                print(f"alpha-desk monitor error: {e}")
            return
        result = {"error": str(e)}
    if brief:
        text = _brief(result, now_ist())
        if text:
            print(text)
        return
    print(json.dumps(result, separators=(",", ":")) if a.cmd in ("dayplan", "fadeplan") else json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
