#!/usr/bin/env python3
"""Offline self-check of fees, sizing rules, the ORB plan, armed orders, exit replay, book accounting and the
backtester. Run: python selftest.py"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

DATA = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402

import backtest as bt  # noqa: E402
import market_data as md  # noqa: E402
import performance as perf  # noqa: E402
import portfolio as pf  # noqa: E402
import strategy  # noqa: E402

# Point every module at a throwaway folder so the checks never touch the real paper book
md.DATA_DIR = pf.DATA_DIR = DATA
pf.PORTFOLIO_FILE = perf.PORTFOLIO_FILE = os.path.join(DATA, "portfolio.json")
pf.DECISIONS_FILE = os.path.join(DATA, "decisions_log.json")
pf.LOCK_FILE = os.path.join(DATA, ".lock")


md.fno_symbols = lambda refresh=False: None  # no F&O file offline: the Nifty 100 stands in for it
L = dict(price=500, stop=494, target=512)  # a 1.2% stop: wide enough that costs stay under a quarter of the risk


def at(h, m):
    return datetime(2026, 10, 30, h, m, tzinfo=md.IST)


def bars(rows, start="2026-10-30 10:16"):
    idx = pd.date_range(start, periods=len(rows), freq="min", tz=md.IST)
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])


def check_costs():
    buy, sell = pf.charges("BUY", 200000), pf.charges("SELL", 200000)
    assert 35 < buy < 38 and 79 < sell < 83, (buy, sell)  # Rs 200k leg: STT only on the sell, stamp duty only on the buy


def check_rules():
    p = pf.plan("LONG", 500, 494, 512, 100000, 0, 5)
    assert not p["problems"] and p["risk"] <= 500 and p["cost_r"] <= pf.MAX_COST_R and p["net_rr"] >= 1.5, p
    assert not pf.plan("SHORT", 500, 506, 488, 100000, 0, 5)["problems"]
    free = pf.plan("LONG", 500, 494, None, 100000, 0, 5)  # no target: the exit is the stop or the square-off
    assert not free["problems"] and "net_rr" not in free, free
    assert "maximum is" in pf.plan("LONG", 500, 480, None, 100000, 0, 5)["problems"][0]  # 4% stop
    assert any("too tight" in x for x in pf.plan("LONG", 500, 499, None, 100000, 0, 5)["problems"])  # costs eat a 0.2% stop
    assert any("reward:risk" in x for x in pf.plan("LONG", 500, 494, 503, 100000, 0, 5)["problems"])
    assert "must be below" in pf.plan("LONG", 500, 505, 510, 100000, 0, 5)["problems"][0]
    assert pf.plan("LONG", 500, 494, None, 100000, 100000, 5)["problems"]  # no free margin


def check_walk():
    long_ = {"side": "LONG", "entry": 500.25, "stop": 494, "stop0": 494, "target": 512}
    assert pf._walk(long_, bars([(500, 500.5, 499, 500), (495, 495, 493, 493.4)]))[:2] == ("stop", 494)
    assert pf._walk(long_, bars([(492, 493, 491, 492)]))[:2] == ("stop", 492)  # gap fills at the open
    assert pf._walk(long_, bars([(500, 513, 500, 512)]))[:2] == ("target", 512)
    assert pf._walk(long_, bars([(500, 513, 493, 500)]))[0] == "stop"  # both touched: stop wins
    r = pf._walk(long_, bars([(500, 508, 500, 507), (501, 501, 500.1, 500.5)]))  # +1R and back: the stop stays put
    assert r[0] is None and r[3] == 494, r
    free = {**long_, "target": None}
    assert pf._walk(free, bars([(500, 560, 500, 555)]))[0] is None  # no target: only the stop or the square-off exits
    short = {"side": "SHORT", "entry": 499.75, "stop": 506, "stop0": 506, "target": 488}
    assert pf._walk(short, bars([(500, 507, 499, 500)]))[:2] == ("stop", 506)
    assert pf._walk(short, bars([(500, 500, 487, 489)]))[:2] == ("target", 488)


def check_book():
    opened = pf.open_position("LONG", "RELIANCE", **L, setup="ORB_PLAY", now=at(10, 15))
    assert opened["status"] == "opened" and opened["risk"] <= 500, opened
    assert "already open" in pf.open_position("LONG", "RELIANCE", **L, now=at(10, 20))["error"]
    assert "entry window" in pf.open_position("LONG", "TCS", **L, now=at(9, 20))["error"]
    assert "entry window" in pf.open_position("LONG", "TCS", **L, now=at(14, 45))["error"]
    assert "exceeds" in pf.open_position("LONG", "TCS", **L, qty=10000, now=at(10, 20))["error"]
    assert "not in F&O" in pf.open_position("SHORT", "NOTASTOCK", 500, 506, 488, now=at(10, 20))["error"]

    closed = pf.exit_position("RELIANCE", 512, "target", now=at(11, 0))
    book = pf.load_portfolio(at(11, 0))
    assert abs(book["cash"] - (pf.CAPITAL + closed["net"])) < 0.01 and closed["net"] > 0 and closed["r"] > 1.5, closed
    assert "already traded today" in pf.open_position("LONG", "RELIANCE", **L, now=at(11, 2))["error"]

    opened = pf.open_position("SHORT", "INFY", 500, 506, 488, now=at(11, 5))
    half = opened["qty"] // 2
    part = pf.exit_position("INFY", 495, "partial", qty=half, now=at(11, 30))
    book = pf.load_portfolio(at(11, 30))
    assert part["qty"] == half and book["positions"][0]["qty"] == opened["qty"] - half and part["net"] > 0, part
    pf.exit_position("INFY", 506, "stop", now=at(11, 40))
    assert len(book["trades"]) == 2 and not pf.load_portfolio(at(11, 40))["positions"]

    book = pf.load_portfolio(at(12, 0))
    book["cash"] = pf.CAPITAL * (1 - pf.DAILY_LOSS_PCT / 100) - 1
    pf.save_json(pf.PORTFOLIO_FILE, book)
    assert "Daily loss limit" in pf.open_position("LONG", "TCS", **L, now=at(12, 0))["error"]


def check_stats():
    assert perf.stats([]) == {"trades": 0}
    s = perf.stats(pf.load_portfolio(at(12, 0))["trades"])
    assert s["trades"] == 3 and s["profit_factor"] > 0, s  # RELIANCE target, INFY partial, INFY stop
    assert "do not change rules" in perf.suggest([{"r": 1}], {"trades": 1, "fees_pct_of_gross": None}, {})[0]


def check_scan():
    prior = pd.date_range("2026-10-20 09:15", periods=8, freq="D", tz=md.IST)
    rows, idx = [], []
    for d in prior:  # eight quiet sessions: 1000 shares in the opening range
        idx += [d, d + pd.Timedelta(minutes=15)]
        rows += [(100, 100.4, 99.8, 100.1, 1000), (100.1, 100.5, 100, 100.2, 800)]
    today = pd.date_range("2026-10-30 09:15", periods=2, freq="15min", tz=md.IST)
    up = pd.DataFrame(rows + [(100, 100.8, 99.9, 100.7, 5000), (100.7, 100.9, 100.5, 100.8, 900)],
                      index=pd.DatetimeIndex(idx).append(today), columns=["Open", "High", "Low", "Close", "Volume"])
    quiet = up.copy()
    quiet.iloc[-2, quiet.columns.get_loc("Volume")] = 1100  # same candle on ordinary volume: not in play
    meta = {k: {"segment": "LARGE", "industry": "X", "atr_pct": 2.0, "prev_close": 100.0, "turnover_cr": 500} for k in ("UP", "QUIET")}
    c = md._candidates({"UP": up, "QUIET": quiet}, meta, at(9, 46))
    assert [x["symbol"] for x in c] == ["UP"] and c[0]["side"] == "LONG" and c[0]["or_rvol"] == 5.0, c
    assert c[0]["trigger"] == 100.85 and c[0]["stop"] == 100.05 and c[0]["state"] == "triggered", c  # stop is 0.4 of a Rs 2 ATR
    assert md._candidates({"UP": up}, meta, at(9, 20)) == []  # the opening range is not complete yet


def check_strategy():
    plan, why = strategy.opening_plan(100, 100.8, 99.9, 99.95, 5000, [1000] * 8, 2.0)
    assert plan["side"] == "SHORT" and plan["trigger"] == 99.85 and plan["stop"] == 100.65, plan  # a red opening candle
    assert strategy.opening_plan(100, 100.8, 99.9, 100.7, 5000, [1000] * 3, 2.0)[1] == "too little volume history"
    assert "ATR" in strategy.opening_plan(100, 103, 99.9, 102.5, 5000, [1000] * 8, 2.0)[1]  # the move is already spent


def check_arm():
    """An approved order waits at its trigger, fills as a stop order would, and never chases."""
    fresh_book()
    quiet = bars([(500, 500.4, 499.6, 500)] * 5, start="2026-10-30 09:46")
    run = bars([(500, 500.4, 499.6, 500), (500.2, 502, 500.1, 501.8), (501.8, 503, 501.5, 502.5)], start="2026-10-30 09:51")
    real = pf.get_bars, pf.get_quotes
    try:
        pf.get_quotes = lambda symbols: [{"symbol": s, "ltp": 500.0, "live": True} for s in symbols]
        armed = pf.arm("LONG", "RELIANCE", 501, 495, now=at(9, 45))
        assert armed["status"] == "armed" and armed["risk"] <= 500, armed
        assert "already has an armed order" in pf.arm("LONG", "RELIANCE", 501, 495, now=at(9, 46))["error"]
        assert "not in F&O" in pf.arm("SHORT", "NOTASTOCK", 499, 505, now=at(9, 46))["error"]
        pf.get_quotes = lambda symbols: [{"symbol": s, "ltp": 503.0, "live": True} for s in symbols]
        assert "do not chase" in pf.arm("LONG", "TCS", 501, 495, now=at(9, 46))["error"]  # a third of the stop past the trigger
        assert "arming window" in pf.arm("LONG", "INFY", 501, 495, now=at(12, 30))["error"]

        pf.get_quotes = lambda symbols: []
        pf.get_bars = lambda symbols, period, interval, now=None: {"RELIANCE": quiet}
        res = pf.monitor(now=at(9, 51))
        assert not res["entries"] and [o["symbol"] for o in res["pending"]] == ["RELIANCE"], res
        pf.get_bars = lambda symbols, period, interval, now=None: {"RELIANCE": pd.concat([quiet, run])}
        res = pf.monitor(now=at(9, 54))
        e = res["entries"][0]
        assert e["symbol"] == "RELIANCE" and 501 < e["entry"] < 501.5 and not res["pending"] and len(res["open"]) == 1, res
        assert pf._brief(res, at(9, 54)).startswith("ENTER RELIANCE LONG")
        book = pf.load_portfolio(at(9, 54))
        assert book["positions"][0]["opened_at"].endswith("09:52:00") and book["positions"][0]["target"] is None, book

        fresh_book()  # an order nobody triggers is cancelled at its cut-off
        pf.get_quotes = lambda symbols: [{"symbol": s, "ltp": 500.0, "live": True} for s in symbols]
        pf.arm("LONG", "TCS", 501, 495, now=at(9, 45))
        pf.get_quotes = lambda symbols: []
        pf.get_bars = lambda symbols, period, interval, now=None: {"TCS": quiet}
        assert not pf.monitor(now=at(12, 1))["pending"] and not pf.load_portfolio(at(12, 1))["positions"]
        assert pf.disarm("all", now=at(12, 2))["symbols"] == []
    finally:
        pf.get_bars, pf.get_quotes = real


def check_backtest():
    """Two symbols over 40 sessions: one breaks out and runs on its heavy-volume days, the other is flat."""
    days = pd.bdate_range("2026-01-05", periods=40)
    def session(day, hot):
        t = pd.date_range(day + pd.Timedelta(hours=9, minutes=15), periods=75, freq="5min")
        px = [100 + (0.03 * i if hot else 0.0) for i in range(76)]
        wig = [0.05 if i < 3 or hot else 1.0 for i in range(75)]  # quiet days still range Rs 2, which sets the ATR
        return pd.DataFrame({"open": px[:-1], "high": [p + w for p, w in zip(px[1:], wig)],
                             "low": [p - w for p, w in zip(px[:-1], wig)],
                             "close": px[1:], "volume": [(9000 if hot else 1000) if i < 3 else 500 for i in range(75)]}, index=t)
    raw = {"RELIANCE": pd.concat([session(d, i >= 25 and i % 3 == 0) for i, d in enumerate(days)]),
           "TCS": pd.concat([session(d, False) for d in days])}
    real = bt.MIN_TURNOVER_CR, bt.MIN_ATR_PCT
    bt.MIN_TURNOVER_CR = bt.MIN_ATR_PCT = 0  # toy volumes
    try:
        trades = bt.simulate(bt.prepare(raw, strategy.PARAMS), strategy.PARAMS)
    finally:
        bt.MIN_TURNOVER_CR, bt.MIN_ATR_PCT = real
    assert len(trades) == 5 and {t["symbol"] for t in trades} == {"RELIANCE"}, trades
    assert all(t["reason"] == "square-off" and t["r"] > 1 for t in trades), trades
    r = bt.report(trades)
    assert r["n"] == 5 and not r["verdict"]["pass"] and "only 5 trades" in r["verdict"]["reasons"][0], r["verdict"]


def fresh_book(*symbols):
    if os.path.exists(pf.PORTFOLIO_FILE):
        os.remove(pf.PORTFOLIO_FILE)
    for s in symbols:
        assert pf.open_position("LONG", s, **L, now=at(10, 15))["status"] == "opened"


def check_monitor():
    flat = lambda n: bars([(500, 500.3, 499.8, 500)] * n, start="2026-10-30 10:17")  # noqa: E731
    gap = bars([(500, 500.5, 499, 500), (465, 466, 464, 465)])  # the 10:17 bar opens 6% under the stop
    real = pf.get_bars, pf.get_quotes
    try:
        pf.get_quotes = lambda symbols: []  # no nse-live price: the bars carry the day
        for when, quiet in ((at(15, 20), flat(300)), (at(12, 0), flat(100))):
            fresh_book("RELIANCE", "TCS")  # real Nifty 100 names, so the large-cap 5x margin leaves room for both
            pf.get_bars = lambda symbols, period, interval, now=None: {"RELIANCE": gap, "TCS": quiet}
            res = pf.monitor(now=when)
            # TCS is closed by the 15:15 square-off in the first pass and by the loss halt in the second
            assert sorted(e["reason"] for e in res["exits"]) == sorted(["stop", "square-off" if when.hour == 15 else "daily-loss-halt"]), res
            assert res["halted"] and res["halt_triggered"] and not res["open"] and res["day_pnl"] < -1500, res
            assert pf.monitor(now=when)["halted"] and not pf.monitor(now=when)["halt_triggered"]  # announced once, not every minute
            assert "Daily loss limit" in pf.open_position("LONG", "INFY", **L, now=at(14, 0))["error"]

        # nse-live is the primary price: with no bars, a live LTP through the stop closes at the market
        fresh_book("RELIANCE")
        pf.get_bars = lambda *a, **k: {}
        pf.get_quotes = lambda symbols: [{"symbol": "RELIANCE", "ltp": 493.0, "live": True}]
        res = pf.monitor(now=at(12, 0))
        assert [e["reason"] for e in res["exits"]] == ["stop"] and res["exits"][0]["exit"] < 493, res

        # no price at all: warn and keep the position, then square off even on a stale official price
        fresh_book("RELIANCE")
        pf.get_quotes = lambda symbols: []
        res = pf.monitor(now=at(12, 0))
        assert res["warnings"] and len(res["open"]) == 1 and not res["exits"], res
        pf.get_quotes = lambda symbols: [{"symbol": "RELIANCE", "ltp": 499.0, "live": False}]
        assert [e["reason"] for e in pf.monitor(now=at(15, 20))["exits"]] == ["square-off"]
    finally:
        pf.get_bars, pf.get_quotes = real


def check_ltp_exit():
    long_ = {"side": "LONG", "stop": 494, "stop0": 494, "target": 512}
    assert pf._ltp_exit(long_, 493) == ("stop", 493) and pf._ltp_exit(long_, 513) == ("target", 512)
    assert pf._ltp_exit(long_, 500) is None and pf._ltp_exit({**long_, "target": None}, 600) is None
    short = {"side": "SHORT", "stop": 506, "stop0": 506, "target": 488}
    assert pf._ltp_exit(short, 507) == ("stop", 507) and pf._ltp_exit(short, 487) == ("target", 488)


RELIANCE = {"stock": {"symbol": "RELIANCE", "series": "EQ", "openPrice": 1170.4, "highPrice": 1174.6, "lowPrice": 1152.5,
                      "preClosePrice": 1167.4, "lastTradedPrice": 1164.9, "perChange": -0.21, "volume": 14753650,
                      "value": 1711.91027045, "latestTimestamp": "2026-10-30 15:31:00"}}  # shaped like a real nse-live reply


def check_nse_client():
    def sse(text, error=False):
        msg = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": text}], "isError": error}}
        return "text/event-stream", "event:message\ndata:" + json.dumps(msg) + "\n\n"

    calls = []

    def flaky(payload, sid=None):
        calls.append(payload["method"])
        if payload["method"] == "initialize":
            return 200, "sid", "application/json", "{}"
        if calls.count("tools/call") < 3:  # no sticky sessions: the follow-up lands on an instance without it
            return 404, None, "", "Session not found"
        return (200, None, *sse('{"ok": 1}'))

    def answers(text, error=False):
        return lambda payload, sid=None: ((200, "sid", "application/json", "{}") if payload["method"] == "initialize"
                                          else (200, None, *sse(text, error)))

    def down(payload, sid=None):
        raise OSError("unreachable")

    real = md._mcp_post
    try:
        md._mcp_post = flaky
        assert md._mcp("cm_get_data_status", {}) == {"ok": 1} and calls.count("initialize") == 3, calls
        md._mcp_post = answers("boom", error=True)
        assert md._mcp("x", {}) is None  # tool errors fall back to Yahoo
        md._mcp_post = answers('{"error": "indexName is required"}')
        assert md._mcp("x", {}) is None
        md._mcp_post = down
        assert md._mcp("x", {}) is None
    finally:
        md._mcp_post = real


def check_nse_quote():
    q = md._norm_quote(RELIANCE, at(15, 33))
    assert q["ltp"] == 1164.9 and abs(q["vwap"] - 1160.33) < 0.01 and q["live"] and q["src"] == "nse-live", q
    assert not md._norm_quote(RELIANCE, at(15, 45))["live"]  # 14 minutes old
    wrong_unit = {"stock": {**RELIANCE["stock"], "value": 1.71e10}}
    assert md._norm_quote(wrong_unit, at(15, 33))["vwap"] is None  # outside the day's range: not trusted
    assert md._norm_quote({"stock": {"symbol": "X", "lastTradedPrice": 0}}, at(10, 0)) is None
    assert md._parse_nse_stamp("30-Oct-2026 15:30:00") == at(15, 30)


def check_quote_fallback():
    stale = md._norm_quote(RELIANCE, at(15, 45))
    yahoo = {"symbol": "RELIANCE", "ltp": 1.0, "live": True, "src": "yfinance"}
    real = md.nse_quotes, md._yahoo_quotes
    try:
        md.nse_quotes = lambda symbols, now=None: [stale] * len(symbols)
        md._yahoo_quotes = lambda symbols, now: {s: yahoo for s in symbols}
        assert md.get_quotes(["RELIANCE.NS"])[0]["src"] == "yfinance"  # a live Yahoo quote beats a stale official one
        md._yahoo_quotes = lambda symbols, now: {}
        assert md.get_quotes(["RELIANCE"])[0]["src"] == "nse-live"  # a stale official one beats nothing
        md.nse_quotes = lambda symbols, now=None: [None] * len(symbols)
        assert "error" in md.get_quotes(["RELIANCE"])[0]
    finally:
        md.nse_quotes, md._yahoo_quotes = real


def check_status():
    real = md.nse_index, md._download, md.now_ist
    md.now_ist = lambda: at(11, 0)
    try:
        md._download = lambda *a, **k: {}
        md.nse_index = lambda name: {"stamp": at(10, 58)}
        s = md.status()
        assert s["state"] == "open" and s["src"] == "nse-live" and s["entry_window"], s
        md.nse_index = lambda name: {"stamp": datetime(2026, 10, 29, 15, 30, tzinfo=md.IST)}  # last value is from yesterday
        s = md.status()
        assert s["state"] == "holiday" and s["error"] == "Market Holiday", s
    finally:
        md.nse_index, md._download, md.now_ist = real


def check_segments():
    uni = md.load_universe()
    assert len(uni) > 480 and sum(v["segment"] == "MID" for v in uni.values()) > 140, len(uni)
    assert md.segment_of("RELIANCE") == "LARGE" and md.segment_of("360ONE.NS") == "MID"
    assert md.segment_of("NOTASTOCK") == "SMALL"  # outside the Nifty 500: the most expensive to trade
    large = pf.plan("LONG", 500, 494, None, 100000, 0, 5, "LARGE")
    mid = pf.plan("LONG", 500, 494, None, 100000, 0, 3, "MID")
    assert mid["cost_r"] > large["cost_r"] and mid["risk"] <= 500 and not mid["problems"], (large, mid)
    assert md.shortable("RELIANCE") and not md.shortable("NOTASTOCK")
    assert md.SHORTLIST_CAPS["SMALL"] == 0  # smallcap costs are a third of a 1% stop


def check_brief():
    quiet = {"entries": [], "exits": [], "warnings": [], "halt_triggered": False, "day_pnl": 0}
    assert pf._brief(quiet, at(11, 0)) == ""  # nothing happened: Hermes stays silent
    stop = {"symbol": "TCS", "side": "LONG", "reason": "stop", "net": -992.3, "r": -1.0}
    text = pf._brief({**quiet, "exits": [stop], "day_pnl": -992.3}, at(11, 0))
    assert text.splitlines()[0] == "EXIT TCS LONG stop: -992 (-1.00R)" and text.endswith("Day P&L -992"), text
    assert "DAILY LOSS LIMIT" in pf._brief({**quiet, "halt_triggered": True, "day_pnl": -2100}, at(11, 0))
    warn = {**quiet, "warnings": ["No price for TCS; position unmanaged"]}  # a data outage alerts once, then every 10 minutes
    assert pf._brief(warn, at(11, 0)) and not pf._brief(warn, at(11, 5)) and pf._brief(warn, at(11, 11))


def check_note():
    fresh_book("RELIANCE")
    assert pf.exit_position("RELIANCE", 494, "stop", now=at(11, 0))["status"] == "closed"
    assert pf.note("RELIANCE", "broke out on fading volume", now=at(11, 5))["status"] == "noted"
    assert pf.load_portfolio(at(11, 5))["trades"][-1]["note"] == "broke out on fading volume"
    pf.note("RELIANCE.NS", "second thought", now=at(11, 6))
    assert pf.load_portfolio(at(11, 6))["trades"][-1]["note"].endswith("; second thought")
    with open(pf.DECISIONS_FILE) as f:
        assert json.load(f)[-1]["type"] == "postmortem"
    assert "error" in pf.note("TCS", "no closed trade", now=at(11, 7))


if __name__ == "__main__":
    try:
        for name, fn in list(globals().items()):
            if name.startswith("check_"):
                fn()
        print("selftest OK")
    finally:
        shutil.rmtree(DATA, ignore_errors=True)
