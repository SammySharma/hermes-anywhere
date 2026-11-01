#!/usr/bin/env python3
"""Paper-trading book for the Alpha Desk NSE intraday (MIS) desk: sizing, entries, exits, stop/target monitor."""

import argparse
import json
import os
import time as _time
from contextlib import contextmanager
from datetime import datetime

from market_data import DATA_DIR, ENTRY_START, IST, LAST_ENTRY, SQUARE_OFF, get_bars, get_quotes, now_ist, segment_of

PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
DECISIONS_FILE = os.path.join(DATA_DIR, "decisions_log.json")
LOCK_FILE = os.path.join(DATA_DIR, ".lock")

# Hard rules. They live here, not in markdown, so the self-improvement loop cannot loosen them.
CAPITAL = 100000
LEVERAGE = 5  # MIS multiplier cap
RISK_PCT = 1.0  # max loss at the stop incl. fees and slippage, % of equity
STOP_MIN_PCT, STOP_MAX_PCT = 0.5, 1.0
MIN_RR = 1.5  # reward:risk after fees and slippage
MAX_POSITIONS = 3
MAX_TRADES_PER_DAY = 6
DAILY_LOSS_PCT = 2.0
# Slippage per fill (against us) and default MIS multiplier by Nifty 500 segment. Midcaps and smallcaps have wider
# spreads and get less leverage. Pass --mis-multiplier with your broker's figure for a stock.
SEGMENTS = {"LARGE": {"slip": 0.03, "mult": 5}, "MID": {"slip": 0.06, "mult": 3}, "SMALL": {"slip": 0.10, "mult": 2}}


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
              "initial_capital": CAPITAL, "cash": CAPITAL, "positions": [], "trades": [],
              "day": {"date": "", "start_equity": CAPITAL, "trades_opened": 0, "halted": False},
              "strategy_version": 1}
    elif "positions" not in pf or pf.get("base_currency") != "INR":
        raise ValueError(f"{PORTFOLIO_FILE} is a legacy swing portfolio. Rename it (e.g. portfolio_v1.json) to start the intraday book.")
    today = now.date().isoformat()
    if pf["day"]["date"] != today:
        pf["day"] = {"date": today, "start_equity": round(pf["cash"], 2), "trades_opened": 0, "halted": False}
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

def plan(side, price, stop, target, equity, margin_used, mult, seg="LARGE"):
    """Size a trade and check the price rules. Returns the numbers plus a list of problems (empty = tradable)."""
    e, x = sides(side)
    sign = 1 if side == "LONG" else -1
    entry = fill(e, price, seg)
    risk_ps = (entry - stop) * sign
    if risk_ps <= 0 or (target - entry) * sign <= 0:
        above, below = ("above", "below") if sign > 0 else ("below", "above")
        return {"problems": [f"For a {side}, the stop must be {below} and the target {above} the entry"]}
    problems = []
    stop_pct = risk_ps / entry * 100
    if not STOP_MIN_PCT <= stop_pct <= STOP_MAX_PCT:
        problems.append(f"Stop is {stop_pct:.2f}% from entry; it must be {STOP_MIN_PCT}-{STOP_MAX_PCT}%")
    budget = equity * RISK_PCT / 100
    cap = min(equity * LEVERAGE / MAX_POSITIONS, (equity - margin_used) * mult)

    def net_at(px, q):
        out = fill(x, px, seg)
        return (out - entry) * q * sign - charges(e, q * entry) - charges(x, q * out)

    qty = int(min(budget // risk_ps, cap // entry))
    while qty > 0 and -net_at(stop, qty) > budget:
        qty -= 1 + qty // 50
    if qty < 1:
        return {"problems": problems + ["Risk budget or free margin is too small for this stop"]}
    risk, reward = -net_at(stop, qty), net_at(target, qty)
    if reward / risk < MIN_RR:
        problems.append(f"Net reward:risk is {reward / risk:.2f}; it must be at least {MIN_RR}")
    return {"entry": round(entry, 2), "qty": qty, "notional": round(qty * entry, 2), "margin": round(qty * entry / mult, 2),
            "stop_pct": round(stop_pct, 2), "risk": round(risk, 2), "reward": round(reward, 2),
            "net_rr": round(reward / risk, 2), "problems": problems}


def _margin_used(pf):
    return sum(p["qty"] * p["entry"] / p["mult"] for p in pf["positions"])


def size(symbol, side, price, stop, target, mult=None, now=None):
    now = now or now_ist()
    pf = load_portfolio(now)
    seg = segment_of(symbol)
    pl = plan(side.upper(), price, stop, target, pf["cash"], _margin_used(pf), min(mult or SEGMENTS[seg]["mult"], LEVERAGE), seg)
    return {"symbol": norm(symbol), "segment": seg, "side": side.upper(), "stop": stop, "target": target, **pl,
            "ok": not pl["problems"]}


# --- entries and exits -----------------------------------------------------------------------------------------------

def open_position(side, symbol, price, stop, target, qty=None, setup="", thesis="", sector="", confidence="medium",
                  mult=None, now=None):
    now = now or now_ist()
    symbol = norm(symbol)
    with lock():
        pf = load_portfolio(now)
        d, t = pf["day"], now.time()
        problems = []
        if not ENTRY_START <= t <= LAST_ENTRY:
            problems.append("Outside the entry window (09:30-14:30 IST)")
        if d["halted"] or pf["cash"] - d["start_equity"] <= -DAILY_LOSS_PCT / 100 * d["start_equity"]:
            problems.append("Daily loss limit reached; no new entries today")
        if d["trades_opened"] >= MAX_TRADES_PER_DAY:
            problems.append(f"Already took {MAX_TRADES_PER_DAY} trades today")
        if len(pf["positions"]) >= MAX_POSITIONS:
            problems.append(f"Already holding {MAX_POSITIONS} positions")
        if any(p["symbol"] == symbol for p in pf["positions"]):
            problems.append(f"{symbol} is already open; no averaging")
        seg = segment_of(symbol)
        mult = min(mult or SEGMENTS[seg]["mult"], LEVERAGE)
        pl = plan(side, price, stop, target, pf["cash"], _margin_used(pf), mult, seg)
        problems += pl["problems"]
        if qty and pl.get("qty") and qty > pl["qty"]:
            problems.append(f"Qty {qty} exceeds the maximum {pl['qty']} for this stop")
        if problems:
            return {"error": "; ".join(problems)}

        qty = qty or pl["qty"]
        e, _ = sides(side)
        pos = {"symbol": symbol, "side": side, "qty": qty, "entry": pl["entry"], "stop": stop, "stop0": stop,
               "target": target, "risk": pl["risk"],  # planned loss at the stop incl. costs: 1R
               "entry_fees": round(charges(e, qty * pl["entry"]), 2), "mult": mult, "segment": seg, "setup": setup,
               "thesis": thesis, "sector": sector, "confidence": confidence, "opened_at": _ts(now), "checked_to": _ts(now)}
        pf["positions"].append(pos)
        d["trades_opened"] += 1
        save_json(PORTFOLIO_FILE, pf)
        log_decision({"type": "trade", "action": "ENTER", "time": _ts(now), "symbol": symbol, "side": side, "qty": qty,
                      "entry": pos["entry"], "stop": stop, "target": target, "setup": setup, "thesis": thesis,
                      "confidence": confidence})
        return {"status": "opened", "symbol": symbol, "segment": seg, "side": side, "qty": qty, "entry": pos["entry"],
                "stop": stop, "target": target, "risk": pl["risk"], "net_rr": pl["net_rr"],
                "margin_used": round(_margin_used(pf), 2)}


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
    """Replay 1-minute bars. Returns (reason, price, time, stop): the first stop or target touch, else reason None with
    the stop moved to cost once price has run +1R. A bar that touches both counts as a stop."""
    sgn = 1 if pos["side"] == "LONG" else -1
    stop, entry, r1 = pos["stop"], pos["entry"], abs(pos["entry"] - pos["stop0"])
    for ts, b in bars.iterrows():
        adverse, favorable = (b["Low"], b["High"]) if sgn > 0 else (b["High"], b["Low"])
        if (adverse - stop) * sgn <= 0:
            px = stop if (b["Open"] - stop) * sgn > 0 else b["Open"]  # a gap through the stop fills at the open
            return ("stop" if stop == pos["stop0"] else "trail-stop"), px, ts, stop
        if (favorable - pos["target"]) * sgn >= 0:
            return "target", pos["target"], ts, stop
        if (stop - entry) * sgn < 0 and (favorable - entry) * sgn >= r1:
            stop = entry
    return None, None, None, stop


def _ltp_exit(pos, px):
    """(reason, fill price) if the latest price is already through the stop or the target, else None."""
    sgn = 1 if pos["side"] == "LONG" else -1
    if (px - pos["stop"]) * sgn <= 0:
        return ("stop" if pos["stop"] == pos["stop0"] else "trail-stop"), px  # already past the stop: fills at the market
    if (px - pos["target"]) * sgn >= 0:
        return "target", pos["target"]
    return None


def monitor(now=None):
    """Book stop/target exits, square off from 15:15, and halt the day at the daily loss limit.
    The latest price comes from nse-live. Yahoo 1-minute bars replay what happened between polls, so a touch of the
    stop or target that has since recovered is not missed, and they stand in for the price when nse-live is down."""
    now = now or now_ist()
    with lock():
        pf = load_portfolio(now)
        d, exits, warn, last = pf["day"], [], [], {}
        was_halted = d["halted"]
        syms = [p["symbol"] for p in pf["positions"]]
        quotes = {q["symbol"]: q for q in get_quotes(syms)} if syms else {}
        bars = get_bars(syms, "1d", "1m", now) if syms else {}
        due = now.time() >= SQUARE_OFF
        for pos in list(pf["positions"]):
            sym, b, q = pos["symbol"], bars.get(pos["symbol"]), quotes.get(pos["symbol"], {})
            px = q["ltp"] if q.get("live") else None
            if b is not None and not b.empty:
                new = b[b.index > _parse(pos["checked_to"])]
                reason, fp, ts, pos["stop"] = _walk(pos, new)
                if reason:
                    exits.append(_close(pf, pos, fp, reason, None, ts))
                    continue
                if not new.empty:
                    pos["checked_to"] = _ts(new.index[-1])
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
            d["halted"] = True
            for pos in list(pf["positions"]):
                if pos["symbol"] in last:
                    exits.append(_close(pf, pos, last[pos["symbol"]], "daily-loss-halt", None, now))
            day_pnl = pf["cash"] - d["start_equity"]
        save_json(PORTFOLIO_FILE, pf)
        return {"time_ist": _ts(now), "exits": exits, "halted": d["halted"], "halt_triggered": d["halted"] and not was_halted,
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
    lines = [f"EXIT {t['symbol']} {t['side']} {t['reason']}: {t['net']:+.0f}" + (f" ({t['r']:+.2f}R)" if t["r"] is not None else "")
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
            "closed_trades": len(pf["trades"]),
            "rules": {"risk_pct": RISK_PCT, "stop_pct": [STOP_MIN_PCT, STOP_MAX_PCT], "min_rr": MIN_RR,
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
    for name in ("size", "long", "short"):
        s = sub.add_parser(name)
        s.add_argument("symbol")
        s.add_argument("price", type=float)
        s.add_argument("--stop", type=float, required=True)
        s.add_argument("--target", type=float, required=True)
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
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
