"""'Chase the winner' demo: each January, put $1,000 into the S&P 500 stock that gained the most
the year before; never sell; reinvest dividends into the same stock. Compared with $1,000 a year
into the S&P 500 (SPY, dividends reinvested). Picks from https://chase-the-winner.vercel.app.

Uses real daily prices and dividends from Yahoo Finance (split-adjusted), cached under
data/demo_cache/. Amounts are US dollars; the exchange rate is fixed at 1 so nothing is converted.
"""
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

from . import config

FIRST_YEAR = 2018  # first January buy
ALL_PICKS = [  # buy year, ticker, name (the site's table, from 2017)
    (2017, "NVDA", "NVIDIA"),
    (2018, "NRG", "NRG Energy"),
    (2019, "AMD", "AMD"),
    (2020, "AMD", "AMD"),
    (2021, "TSLA", "Tesla"),
    (2022, "DVN", "Devon Energy"),
    (2023, "OXY", "Occidental Petroleum"),
    (2024, "NVDA", "NVIDIA"),
    (2025, "PLTR", "Palantir"),
    (2026, "SNDK", "Sandisk"),
]
PICKS = [p for p in ALL_PICKS if p[0] >= FIRST_YEAR]
INDEX = "SPY"
AMOUNT = 1000.0
ACCOUNTS = [("70000001", "Winners"), ("70000002", "S&P 500")]
DEMO_CONFIG = {
    "history_start": f"{FIRST_YEAR}-01-01",
    "tfsa_eligible_since": None,
    "tfsa_room_from_cra": None,
    "tfsa_nonresident_years": [],
    "asset_classes": {s: "Equities" for s in {t for _, t, _ in PICKS} | {INDEX}},
}
TITLE = "Chase the winner"
NOTE = (f"Each January 1 from {FIRST_YEAR}, $1,000 into the S&P 500 stock that gained the most the year before, never sold, "
        "dividends reinvested; compared with $1,000 a year into the S&P 500 (SPY). Real prices from Yahoo "
        "Finance; amounts in US dollars. Picks from chase-the-winner.vercel.app.")
NAMES = {t: n for _, t, n in PICKS} | {INDEX: "SPDR S&P 500 ETF"}
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}"


def fetch(symbol, start, end):
    """Daily closes and dividends, split-adjusted: ({date: close}, {date: dividend per share})."""
    cache = config.ROOT / "data" / "demo_cache" / f"{symbol}_{end.isoformat()}.json"
    if cache.exists():
        raw = json.loads(cache.read_text())
    else:
        r = requests.get(YAHOO.format(symbol), timeout=30, headers={"User-Agent": "Mozilla/5.0"},
                         params={"period1": int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()),
                                 "period2": int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400,
                                 "interval": "1d", "events": "div|split"})
        r.raise_for_status()
        raw = r.json()["chart"]["result"][0]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(raw))
    offset = raw["meta"].get("gmtoffset", 0)
    day = lambda ts: datetime.fromtimestamp(ts + offset, timezone.utc).date()
    closes = {day(ts): c for ts, c in zip(raw["timestamp"], raw["indicators"]["quote"][0]["close"]) if c}
    divs = {day(v["date"]): v["amount"] for v in raw.get("events", {}).get("dividends", {}).values()}
    return closes, divs


def stamp(d):
    return f"{d.isoformat()}T00:00:00.000000-05:00"


def row(d, type_, action, desc, net, symbol="", qty=0.0, price=0.0):
    return {"tradeDate": stamp(d), "transactionDate": stamp(d), "settlementDate": stamp(d), "action": action,
            "symbol": symbol, "symbolId": 0, "description": desc, "currency": "USD", "quantity": round(qty, 6),
            "price": round(price, 4), "grossAmount": round(-qty * price, 2), "commission": 0,
            "netAmount": round(net, 2), "type": type_}


def first_close_on_or_after(closes, d):
    days = sorted(k for k in closes if k >= d)
    return days[0], closes[days[0]]


def close_on_or_before(closes, d):
    days = [k for k in closes if k <= d]
    return closes[max(days)]


def account_rows(buys, data, today):
    """buys: [(year, symbol)] -> activity rows, final shares per symbol, and per-buy share lots."""
    events = []  # (date, order, kind, symbol, lot)
    for i, (year, sym) in enumerate(buys):
        d, _ = first_close_on_or_after(data[sym][0], date(year, 1, 1))
        events.append((d, 0, "buy", sym, i))
    for sym in {s for _, s in buys}:
        for d, amt in data[sym][1].items():
            events.append((d, 1, "div", sym, amt))
    events.sort(key=lambda e: (e[0], e[1]))
    rows, shares, lots = [], defaultdict(float), defaultdict(float)
    for d, _, kind, sym, x in events:
        if d > today:
            continue
        closes = data[sym][0]
        if kind == "buy":
            price = closes[d]
            q = AMOUNT / price
            rows.append(row(d, "Deposits", "CON", "DEPOSIT", AMOUNT))
            rows.append(row(d, "Trades", "Buy", f"{NAMES.get(sym, sym).upper()} WE ACTED AS AGENT", -AMOUNT, sym, q, price))
            shares[sym] += q
            lots[x] += q
        elif shares[sym] > 0:  # dividend on shares held before the ex-date, reinvested the same day
            cash = shares[sym] * x
            price = close_on_or_before(closes, d)
            q = cash / price
            rows.append(row(d, "Dividends", "DIV", f"{NAMES.get(sym, sym).upper()} CASH DIV ON {shares[sym]:.4f} SHS", cash, sym))
            rows.append(row(d, "Dividend reinvestment", "REI", f"{NAMES.get(sym, sym).upper()} REINV@{price:.2f}", -cash, sym, q, price))
            for i, (_, s) in enumerate(buys):  # each lot grows by its share of the reinvestment
                if s == sym and lots[i]:
                    lots[i] += lots[i] / shares[sym] * q
            shares[sym] += q
    return rows, shares, lots


def build(store, today=None):
    today = today or date.today()
    syms = sorted({s for _, s, _ in PICKS} | {INDEX})
    start = date(FIRST_YEAR - 1, 12, 1)
    data = {s: fetch(s, start, today) for s in syms}
    last = min(max(c) for c, _ in data.values())  # latest day every series has a price for
    store.save_fx([((start + timedelta(days=i)).isoformat(), 1.0) for i in range((today - start).days + 1)])
    for s in syms:
        store.save_symbol(s, 0, "USD")
        store.save_prices(s, [(d.isoformat(), c) for d, c in sorted(data[s][0].items())])
    plans = {"70000001": [(y, s) for y, s, _ in PICKS], "70000002": [(y, INDEX) for y, _, _ in PICKS]}
    lots_out = {}
    for number, acct_type in ACCOUNTS:
        rows, shares, lots = account_rows(plans[number], data, last)
        lots_out[number] = lots
        store.save_account({"number": number, "type": acct_type, "status": "Active", "isPrimary": False})
        store.replace_activities(number, "0000-00-00", rows)
        positions, equity = [], 0.0
        for s, q in shares.items():
            price = close_on_or_before(data[s][0], last)
            cost = sum(-r["netAmount"] for r in rows if r["symbol"] == s and r["type"] in ("Trades", "Dividend reinvestment"))
            positions.append({"symbol": s, "symbolId": 0, "openQuantity": round(q, 6), "currentPrice": round(price, 2),
                              "currentMarketValue": round(q * price, 2), "averageEntryPrice": round(cost / q, 4),
                              "totalCost": round(cost, 2), "openPnl": round(q * price - cost, 2)})
            equity += q * price
        balances = {"perCurrencyBalances": [{"currency": "USD", "cash": 0.0}, {"currency": "CAD", "cash": 0.0}],
                    "combinedBalances": [{"currency": "CAD", "totalEquity": round(equity, 2)},
                                         {"currency": "USD", "totalEquity": round(equity, 2)}]}
        store.save_snapshot(number, balances, positions, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    # Per-pick results, like the site's table: what each $1,000 became, against the same $1,000 in SPY.
    picks = []
    for i, (year, sym, name) in enumerate(PICKS):
        now = lots_out["70000001"][i] * close_on_or_before(data[sym][0], last)
        idx = lots_out["70000002"][i] * close_on_or_before(data[INDEX][0], last)
        picks.append({"year": year, "symbol": sym, "name": name, "now": round(now, 2), "index": round(idx, 2)})
    store.set_meta("demo_title", TITLE)
    store.set_meta("demo_note", NOTE)
    store.set_meta("demo_currency", "USD")
    store.set_meta("demo_picks", json.dumps(picks))
    store.set_meta("demo_prices_as_of", last.isoformat())
    store.set_meta("last_sync", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    store.commit()
    return picks
