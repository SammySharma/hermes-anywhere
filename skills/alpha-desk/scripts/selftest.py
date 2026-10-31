#!/usr/bin/env python3
"""Offline self-check of fees, sizing rules, exit replay, book accounting and the setup scan. Run: python selftest.py"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

DATA = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402

import market_data as md  # noqa: E402
import performance as perf  # noqa: E402
import portfolio as pf  # noqa: E402

# Point every module at a throwaway folder so the checks never touch the real paper book
md.DATA_DIR = pf.DATA_DIR = DATA
pf.PORTFOLIO_FILE = perf.PORTFOLIO_FILE = os.path.join(DATA, "portfolio.json")
pf.DECISIONS_FILE = os.path.join(DATA, "decisions_log.json")
pf.LOCK_FILE = os.path.join(DATA, ".lock")


def at(h, m):
    return datetime(2026, 10, 30, h, m, tzinfo=md.IST)


def bars(rows, start="2026-10-30 10:16"):
    idx = pd.date_range(start, periods=len(rows), freq="min", tz=md.IST)
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])


def check_costs():
    buy, sell = pf.charges("BUY", 200000), pf.charges("SELL", 200000)
    assert 35 < buy < 38 and 79 < sell < 83, (buy, sell)  # Rs 200k leg: STT only on the sell, stamp duty only on the buy


def check_rules():
    p = pf.plan("LONG", 500, 497.5, 507.5, 100000, 0, 5)
    assert not p["problems"] and p["risk"] <= 1000 and p["notional"] <= 100000 * 5 / 3 and p["net_rr"] >= 1.5, p
    assert not pf.plan("SHORT", 500, 502.5, 492.5, 100000, 0, 5)["problems"]
    assert "Stop is" in pf.plan("LONG", 500, 490, 520, 100000, 0, 5)["problems"][0]  # 2% stop
    assert "Stop is" in pf.plan("LONG", 500, 499, 510, 100000, 0, 5)["problems"][0]  # 0.2% stop
    assert any("reward:risk" in x for x in pf.plan("LONG", 500, 497.5, 502, 100000, 0, 5)["problems"])
    assert "must be below" in pf.plan("LONG", 500, 505, 510, 100000, 0, 5)["problems"][0]
    assert pf.plan("LONG", 500, 497.5, 507.5, 100000, 100000, 5)["problems"]  # no free margin


def check_walk():
    long_ = {"side": "LONG", "entry": 500.15, "stop": 497.5, "stop0": 497.5, "target": 507.5}
    assert pf._walk(long_, bars([(500, 500.5, 499, 500), (498, 498, 497, 497.4)]))[:2] == ("stop", 497.5)
    assert pf._walk(long_, bars([(496, 497, 495, 496)]))[:2] == ("stop", 496)  # gap fills at the open
    assert pf._walk(long_, bars([(500, 508, 500, 507)]))[:2] == ("target", 507.5)
    assert pf._walk(long_, bars([(500, 508, 497, 500)]))[0] == "stop"  # both touched: stop wins
    r = pf._walk(long_, bars([(500, 503, 500, 502), (501, 501, 500.1, 500.5)]))  # +1R moves the stop to cost
    assert r[:2] == ("trail-stop", 500.15), r
    short = {"side": "SHORT", "entry": 499.85, "stop": 502.5, "stop0": 502.5, "target": 492.5}
    assert pf._walk(short, bars([(500, 503, 499, 500)]))[:2] == ("stop", 502.5)
    assert pf._walk(short, bars([(500, 500, 492, 493)]))[:2] == ("target", 492.5)


def check_book():
    opened = pf.open_position("LONG", "ABC", 500, 497.5, 507.5, setup="ORB_LONG", now=at(10, 15))
    assert opened["status"] == "opened" and opened["risk"] <= 1000, opened
    assert "already open" in pf.open_position("LONG", "ABC", 500, 497.5, 507.5, now=at(10, 20))["error"]
    assert "entry window" in pf.open_position("LONG", "XYZ", 500, 497.5, 507.5, now=at(9, 20))["error"]
    assert "entry window" in pf.open_position("LONG", "XYZ", 500, 497.5, 507.5, now=at(14, 45))["error"]
    assert "exceeds" in pf.open_position("LONG", "XYZ", 500, 497.5, 507.5, qty=10000, now=at(10, 20))["error"]

    closed = pf.exit_position("ABC", 507.5, "target", now=at(11, 0))
    book = pf.load_portfolio(at(11, 0))
    assert abs(book["cash"] - (pf.CAPITAL + closed["net"])) < 0.01 and closed["net"] > 0 and closed["r"] > 1.5, closed

    opened = pf.open_position("SHORT", "DEF", 500, 502.5, 492.5, now=at(11, 5))
    half = opened["qty"] // 2
    part = pf.exit_position("DEF", 495, "partial", qty=half, now=at(11, 30))
    book = pf.load_portfolio(at(11, 30))
    assert part["qty"] == half and book["positions"][0]["qty"] == opened["qty"] - half and part["net"] > 0, part
    pf.exit_position("DEF", 502.5, "stop", now=at(11, 40))
    assert len(book["trades"]) == 2 and not pf.load_portfolio(at(11, 40))["positions"]

    book = pf.load_portfolio(at(12, 0))
    book["cash"] = pf.CAPITAL * (1 - pf.DAILY_LOSS_PCT / 100) - 1
    pf.save_json(pf.PORTFOLIO_FILE, book)
    assert "Daily loss limit" in pf.open_position("LONG", "GHI", 500, 497.5, 507.5, now=at(12, 0))["error"]


def check_scan():
    prior = pd.date_range("2026-10-29 09:15", periods=25, freq="15min", tz=md.IST)
    today = pd.date_range("2026-10-30 09:15", periods=3, freq="15min", tz=md.IST)
    rows = [(100, 101, 99.5, 100.5, 500)] * 25 + [(100, 101, 99.5, 100.5, 1000), (100.5, 101, 100.2, 100.8, 800),
                                                  (100.8, 102, 100.8, 101.8, 2000)]
    df = pd.DataFrame(rows, index=prior.append(today), columns=["Open", "High", "Low", "Close", "Volume"])
    m = md._metrics(df, 100.0, at(10, 0))  # bars up to 09:45 are complete
    assert "ORB_LONG" in m["signals"] and m["rvol"] > 2, m
    assert md._metrics(df, 100.0, at(9, 30))["signals"] == []  # only the opening-range bar exists


def check_stats():
    assert perf.stats([]) == {"trades": 0}
    s = perf.stats(pf.load_portfolio(at(12, 0))["trades"])
    assert s["trades"] == 3 and s["profit_factor"] > 0, s  # ABC target, DEF partial, DEF stop
    assert "do not change rules" in perf.suggest([{"r": 1}], {"trades": 1, "fees_pct_of_gross": None}, {})[0]


def fresh_book(*symbols):
    if os.path.exists(pf.PORTFOLIO_FILE):
        os.remove(pf.PORTFOLIO_FILE)
    for s in symbols:
        assert pf.open_position("LONG", s, 500, 497.5, 507.5, now=at(10, 15))["status"] == "opened"


def check_monitor():
    flat = lambda n: bars([(500, 500.3, 499.8, 500)] * n, start="2026-10-30 10:17")  # noqa: E731
    gap = bars([(500, 500.5, 499, 500), (480, 481, 479, 480)])  # the 10:17 bar opens 4% under the stop
    real = pf.get_bars, pf.get_quotes
    try:
        pf.get_quotes = lambda symbols: []  # no nse-live price: the bars carry the day
        for when, quiet in ((at(15, 20), flat(300)), (at(12, 0), flat(100))):
            fresh_book("RELIANCE", "TCS")  # real Nifty 100 names, so the large-cap 5x margin leaves room for both
            pf.get_bars = lambda symbols, period, interval, now=None: {"RELIANCE": gap, "TCS": quiet}
            res = pf.monitor(now=when)
            # TCS is closed by the 15:15 square-off in the first pass and by the loss halt in the second
            assert sorted(e["reason"] for e in res["exits"]) == sorted(["stop", "square-off" if when.hour == 15 else "daily-loss-halt"]), res
            assert res["halted"] and not res["open"] and res["day_pnl"] < -2000, res
            assert "Daily loss limit" in pf.open_position("LONG", "INFY", 500, 497.5, 507.5, now=at(14, 0))["error"]

        # nse-live is the primary price: with no bars, a live LTP through the stop closes at the market
        fresh_book("RELIANCE")
        pf.get_bars = lambda *a, **k: {}
        pf.get_quotes = lambda symbols: [{"symbol": "RELIANCE", "ltp": 496.0, "live": True}]
        res = pf.monitor(now=at(12, 0))
        assert [e["reason"] for e in res["exits"]] == ["stop"] and res["exits"][0]["exit"] < 496, res

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
    long_ = {"side": "LONG", "stop": 497.5, "stop0": 497.5, "target": 507.5}
    assert pf._ltp_exit(long_, 497) == ("stop", 497) and pf._ltp_exit(long_, 508) == ("target", 507.5)
    assert pf._ltp_exit(long_, 500) is None and pf._ltp_exit({**long_, "stop": 500.15}, 500) == ("trail-stop", 500)
    short = {"side": "SHORT", "stop": 502.5, "stop0": 502.5, "target": 492.5}
    assert pf._ltp_exit(short, 503) == ("stop", 503) and pf._ltp_exit(short, 492) == ("target", 492.5)


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
    large = pf.plan("LONG", 500, 497.5, 507.5, 100000, 0, 5, "LARGE")
    mid = pf.plan("LONG", 500, 497.5, 507.5, 100000, 0, 3, "MID")
    assert mid["net_rr"] < large["net_rr"] and mid["risk"] <= 1000 and not mid["problems"], (large, mid)


if __name__ == "__main__":
    try:
        for name, fn in list(globals().items()):
            if name.startswith("check_"):
                fn()
        print("selftest OK")
    finally:
        shutil.rmtree(DATA, ignore_errors=True)
