"""Made-up portfolio for demos and screenshots: no real account data, no network.

Builds a separate database (data/demo.db) shaped exactly like a Questrade sync: three accounts,
monthly deposits and purchases, dividends (with US tax withheld outside the RRSP), a withdrawal,
a fund switch, simulated daily prices and exchange rates. Same seed, same data every time.
"""
import math
import random
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

START = date(2019, 1, 2)

# symbol: (symbolId, currency, description, start price, yearly drift, yearly volatility, yield, first day)
FUNDS = {
    "XEQT.TO": (900001, "CAD", "ISHARES CORE EQUITY ETF PORTFOLIO", 20.0, 0.08, 0.15, 0.018, START),
    "VFV.TO": (900002, "CAD", "VANGUARD S&P 500 INDEX ETF", 62.0, 0.11, 0.16, 0.011, START),
    "ZAG.TO": (900003, "CAD", "BMO AGGREGATE BOND INDEX ETF", 15.5, 0.00, 0.05, 0.032, START),
    "VTI": (900004, "USD", "VANGUARD TOTAL STOCK MARKET ETF", 130.0, 0.10, 0.17, 0.014, START),
    "IBIT": (900005, "USD", "ISHARES BITCOIN TRUST ETF", 25.0, 0.25, 0.55, 0.0, date(2024, 1, 11)),
}
ACCOUNTS = [  # number, type, opened, monthly deposit, what deposits buy (symbol: share of deposit)
    ("11112222", "TFSA", START, 550, {"XEQT.TO": 0.6, "VTI": 0.4}),
    ("33334444", "RRSP", START, 700, {"VTI": 0.5, "ZAG.TO": 0.5}),
    ("55556666", "FHSA", date(2023, 4, 3), 0, {"VFV.TO": 1.0}),
]
DEMO_CONFIG = {
    "history_start": START.isoformat(),
    "tfsa_eligible_since": "2012",
    "tfsa_room_from_cra": None,
    "tfsa_nonresident_years": [],
}


def business_days(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def simulate(rng, today):
    """Daily closes per symbol and USD/CAD rates, as random walks."""
    prices = {}
    for sym, (_, _, _, p0, drift, vol, _, first) in FUNDS.items():
        series, p = {}, p0
        for d in business_days(first, today):
            p *= math.exp((drift - vol * vol / 2) / 252 + vol / math.sqrt(252) * rng.gauss(0, 1))
            series[d] = round(p, 2)
        prices[sym] = series
    fx, r = {}, 1.33
    for d in business_days(START, today):
        r = min(1.46, max(1.24, r * math.exp(0.004 * rng.gauss(0, 1))))
        fx[d] = round(r, 4)
    return prices, fx


def stamp(d):
    return f"{d.isoformat()}T00:00:00.000000-05:00"


def row(d, type_, action, desc, currency, net, symbol="", qty=0.0, price=0.0):
    sid = FUNDS[symbol][0] if symbol else 0
    return {"tradeDate": stamp(d), "transactionDate": stamp(d), "settlementDate": stamp(d), "action": action,
            "symbol": symbol, "symbolId": sid, "description": desc, "currency": currency, "quantity": qty,
            "price": price, "grossAmount": round(-qty * price, 2) if qty else 0, "commission": 0,
            "netAmount": round(net, 2), "type": type_}


def at(series, d):
    while d not in series:
        d -= timedelta(days=1)
    return series[d]


def build_activities(rng, prices, fx, today):
    acts = {}
    for number, acct_type, opened, monthly, targets in ACCOUNTS:
        rows, cash, qty = [], defaultdict(float), defaultdict(float)

        def deposit(d, amount):
            rows.append(row(d, "Deposits", "CON", f"CONT {number}", "CAD", amount))
            cash["CAD"] += amount

        def buy(d, sym, budget_cad):
            _, cur, desc, *_ = FUNDS[sym]
            price = at(prices[sym], d)
            if cur == "USD":  # convert first, as Questrade records it: two FX legs
                rate = at(fx, d) * 1.015  # broker spread
                usd = round(budget_cad / rate, 2)
                rows.append(row(d, "FX conversion", "FXT", "CONVERSION - CAD/USD", "CAD", -budget_cad))
                rows.append(row(d, "FX conversion", "FXT", "CONVERSION - CAD/USD", "USD", usd))
                cash["CAD"] -= budget_cad
                cash["USD"] += usd
                budget = cash["USD"]  # spend any US dividends sitting in cash too
            else:
                budget = budget_cad
            shares = math.floor(budget / price)
            if shares <= 0:
                return
            cost = shares * price
            rows.append(row(d, "Trades", "Buy", f"{desc} WE ACTED AS AGENT", cur, -cost, sym, shares, price))
            cash[cur] -= cost
            qty[sym] += shares

        def sell(d, sym, shares):
            _, cur, desc, *_ = FUNDS[sym]
            price = at(prices[sym], d)
            rows.append(row(d, "Trades", "Sell", f"{desc} WE ACTED AS AGENT", cur, shares * price, sym, -shares, price))
            cash[cur] += shares * price
            qty[sym] -= shares

        fhsa_years = set()
        d = opened
        while d <= today:
            if acct_type == "FHSA" and d.month == opened.month and d.weekday() < 5 and d.year not in fhsa_years:
                fhsa_years.add(d.year)
                deposit(d, 8000)  # one yearly FHSA deposit, the full annual limit
                for sym, share in targets.items():
                    buy(d, sym, 8000 * share)
            elif monthly and d.day <= 7 and d.weekday() < 5 and not any(r["tradeDate"][:7] == d.isoformat()[:7] and r["type"] == "Deposits" for r in rows):
                extra = 3000 if d.month == 1 else 0  # bigger January top-ups
                amount = monthly + extra
                deposit(d, amount)
                spend = cash["CAD"]  # the deposit plus any Canadian dividends waiting in cash
                for sym, share in targets.items():
                    if sym in FUNDS and FUNDS[sym][7] <= d:
                        buy(d, sym, spend * share)
            # quarterly distributions on whatever is held
            if d.month in (3, 6, 9, 12) and d.day == 28:
                for sym, q in list(qty.items()):
                    _, cur, desc, _, _, _, yld, _ = FUNDS[sym]
                    if q <= 0 or not yld:
                        continue
                    gross = q * at(prices[sym], d) * yld / 4
                    if cur == "USD" and acct_type != "RRSP":  # treaty exemption only for the RRSP
                        rows.append(row(d, "Dividends", "DIV", f"{desc} CASH DIV ON {q:g} SHS NON-RES TAX WITHH",
                                        cur, gross * 0.85))
                        cash[cur] += gross * 0.85
                    else:
                        rows.append(row(d, "Dividends", "DIV", f"{desc} CASH DIV ON {q:g} SHS", cur, gross))
                        cash[cur] += gross
            # one TFSA withdrawal, and a switch into the bitcoin fund
            if acct_type == "TFSA" and d == date(2022, 5, 9):
                sell(d, "XEQT.TO", 300)
                w = round(min(cash["CAD"], 7000), 2)
                rows.append(row(d, "Withdrawals", "WDR", "ELECTRONIC FUND TRANSFER", "CAD", -w))
                cash["CAD"] -= w
            if acct_type == "TFSA" and d == date(2024, 3, 4):
                sell(d, "XEQT.TO", 250)
                buy(d, "IBIT", cash["CAD"] * 0.95)
            d += timedelta(days=1)
        acts[number] = (rows, cash, qty)
    return acts


def snapshot(rows, cash, qty, prices, fx, today):
    rate = at(fx, today)
    positions, bought = [], defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        if r["type"] == "Trades" and r["quantity"] > 0:
            bought[r["symbol"]][0] += r["quantity"]
            bought[r["symbol"]][1] += r["quantity"] * r["price"]
    equity = cash["CAD"] + cash["USD"] * rate
    for sym, q in qty.items():
        if q <= 0:
            continue
        cur, price = FUNDS[sym][1], at(prices[sym], today)
        avg = bought[sym][1] / bought[sym][0]
        positions.append({"symbol": sym, "symbolId": FUNDS[sym][0], "openQuantity": q, "currentPrice": price,
                          "currentMarketValue": round(q * price, 2), "averageEntryPrice": round(avg, 4),
                          "totalCost": round(q * avg, 2), "openPnl": round(q * (price - avg), 2)})
        equity += q * price * (rate if cur == "USD" else 1)
    balances = {"perCurrencyBalances": [{"currency": c, "cash": round(cash[c], 2)} for c in ("CAD", "USD")],
                "combinedBalances": [{"currency": "CAD", "totalEquity": round(equity, 2)},
                                     {"currency": "USD", "totalEquity": round(equity / rate, 2)}]}
    return balances, positions


def build(store, today=None):
    today = today or date.today()
    rng = random.Random(20190102)
    prices, fx = simulate(rng, today)
    store.save_fx([(d.isoformat(), r) for d, r in fx.items()])
    for sym, series in prices.items():
        store.save_symbol(sym, FUNDS[sym][0], FUNDS[sym][1])
        store.save_prices(sym, [(d.isoformat(), p) for d, p in series.items()])
    for (number, acct_type, *_), (rows, cash, qty) in zip(ACCOUNTS, build_activities(rng, prices, fx, today).values()):
        store.save_account({"number": number, "type": acct_type, "status": "Active", "isPrimary": False})
        store.replace_activities(number, "0000-00-00", rows)
        balances, positions = snapshot(rows, cash, qty, prices, fx, today)
        store.save_snapshot(number, balances, positions, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    store.set_meta("last_sync", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    store.commit()
