"""Everything the dashboard shows, computed from the local store."""
import re
from collections import defaultdict
from datetime import date, timedelta

from . import assets, config
from .ledger import TREATY_EXEMPT, Ledger

US_WITHHOLDING = 0.15
FALLBACK_USDCAD = 1.35

FHSA_ANNUAL, FHSA_CARRY_MAX, FHSA_LIFETIME = 8000, 8000, 40000
TFSA_LIMITS = {**{y: 5000 for y in range(2009, 2013)}, 2013: 5500, 2014: 5500, 2015: 10000,
               2016: 5500, 2017: 5500, 2018: 5500, **{y: 6000 for y in range(2019, 2023)},
               2023: 6500, 2024: 7000, 2025: 7000, 2026: 7000}


_AS_OF = re.compile(r"\bAS OF (\d{2})/(\d{2})/(\d{2})\b")


def fx_date(act):
    """Date whose exchange rate applies. A correction such as a cancelled deposit says
    'AS OF 02/21/19'; converting it at the original date makes it cancel exactly."""
    m = _AS_OF.search(act.description.upper())
    return f"20{m[3]}-{m[1]}-{m[2]}" if m else act.date


def xirr(flows, value, end):
    """Money-weighted annual return. flows: [(date, amount)] with money put in positive."""
    if not flows:
        return None
    end_t = date.fromisoformat(end)
    yrs = [((end_t - date.fromisoformat(d)).days / 365.25, c) for d, c in flows]

    def f(r):
        return sum(c * (1 + r) ** t for t, c in yrs) - value
    lo, hi = -0.99, 10.0
    if f(lo) * f(hi) > 0:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def build(store, today=None):
    cfg = config.load()
    today = today or date.today().isoformat()
    skip = set(cfg["skip_accounts"])
    accounts = [a for a in store.accounts() if a["number"] not in skip]
    ledger = Ledger(accounts, {a["number"]: store.activities(a["number"]) for a in accounts},
                    cfg["symbol_aliases"])
    fx = store.fx_table()
    prices = store.price_tables()
    issues = []
    if not fx:
        issues.append(f"No Bank of Canada exchange rates yet; using {FALLBACK_USDCAD} for USD. Sync to fix.")

    def usdcad(d):
        return fx.at(d) if fx else FALLBACK_USDCAD

    def cad(amount, currency, d):
        return amount * usdcad(d) if currency == "USD" else amount

    acts = ledger.acts
    nums = [a["number"] for a in accounts]
    types = {a["number"]: a["type"] for a in accounts}
    year_now = int(today[:4])
    ttm_start = (date.fromisoformat(today) - timedelta(days=365)).isoformat()

    # --- money in and out ---
    flows = defaultdict(list)  # account -> [(date, cad)]
    for a in acts:
        if a.kind in ("contribution", "withdrawal", "transfer") and a.net:
            flows[a.account].append((a.date, cad(a.net, a.currency, fx_date(a))))
        if a.kind == "transfer" and a.qty:
            issues.append(f"{types[a.account]}: shares transferred in on {a.date} are not counted as money put in.")

    # --- value over time ---
    first = acts[0].date if acts else today
    dates, d = [], date.fromisoformat(first)
    while d.isoformat() < today:
        dates.append(d.isoformat())
        d += timedelta(days=7)
    dates.append(today)
    approx, partial = set(), set()  # no price history at all / history starts after you held it

    def price(sym, d):
        t = prices.get(sym)
        if t and t.dates[0] <= d:
            return t.at(d)
        (partial if t else approx).add(sym)
        return ledger.last_trade_price.get(sym, 0.0)

    series_value = {n: [] for n in nums}
    series_in = {n: [] for n in nums}
    flow_i = {n: 0 for n in nums}
    running_in = defaultdict(float)
    end_pos = end_cash = None
    for d, pos, cash in ledger.walk(dates):
        for n in nums:
            fl = flows[n]
            while flow_i[n] < len(fl) and fl[flow_i[n]][0] <= d:
                running_in[n] += fl[flow_i[n]][1]
                flow_i[n] += 1
            v = sum(cad(q * price(s, d), ledger.symbol_currency.get(s, "CAD"), d)
                    for s, q in pos[n].items() if abs(q) > 1e-9)
            v += sum(cad(c, cur, d) for cur, c in cash[n].items())
            series_value[n].append(round(v, 2))
            series_in[n].append(round(running_in[n], 2))
        end_pos, end_cash = pos, cash
    if approx:
        fix = ("Your Questrade app doesn't have market data permission: in App Hub, turn on 'Retrieve "
               "delayed and real-time market data', then generate and paste a new token."
               if store.meta("price_error") == "scope" else "Sync to fetch prices.")
        issues.append("No price history for " + ", ".join(sorted(approx)) +
                      "; the value line uses their last trade price. " + fix)
    if partial:
        issues.append("Questrade's price history for " + ", ".join(sorted(partial)) + " starts after you "
                      "first held it; for those earlier weeks the value line uses the last trade price.")

    # --- today, from Questrade's own balances, and a check that the ledger agrees ---
    acct_out, holdings = [], []
    fx_now = usdcad(today)
    last_sync = store.meta("last_sync")
    for n in nums:
        bal, positions, taken = store.snapshot(n)
        value = None
        if bal:
            value = next((b["totalEquity"] for b in bal["combinedBalances"] if b["currency"] == "CAD"), None)
            last_sync = last_sync or taken
        if value is None:
            value = series_value[n][-1]
        series_value[n][-1] = round(value, 2)  # end the line on the broker's figure
        money_in = series_in[n][-1]
        cash_cad = sum(cad(b["cash"], b["currency"], today) for b in (bal or {}).get("perCurrencyBalances", []))
        acct_out.append({"number": n, "type": types[n], "value": value, "money_in": money_in, "cash": cash_cad,
                         "gain": value - money_in,
                         "xirr": xirr(flows[n], value, today)})
        for p in positions or []:
            cur = ledger.symbol_currency.get(p["symbol"], "USD" if not p["symbol"].endswith(".TO") else "CAD")
            holdings.append({"account": types[n], "number": n, "symbol": p["symbol"], "qty": p["openQuantity"],
                             "price": p["currentPrice"], "currency": cur,
                             "value_cad": cad(p["currentMarketValue"] or 0, cur, today),
                             "cost_cad": cad(p["totalCost"] or 0, cur, today),
                             "avg_cost": p["averageEntryPrice"]})
            q = end_pos[n].get(p["symbol"], 0.0) if end_pos else 0.0
            if abs(q - p["openQuantity"]) > 1e-4:
                issues.append(f"{types[n]} {p['symbol']}: activity history adds up to {q:g} shares, "
                              f"Questrade reports {p['openQuantity']:g}.")
        for b in (bal or {}).get("perCurrencyBalances", []):
            c = end_cash[n].get(b["currency"], 0.0) if end_cash else 0.0
            if abs(c - b["cash"]) > 0.05:
                issues.append(f"{types[n]} {b['currency']} cash: activity history adds up to {c:,.2f}, "
                              f"Questrade reports {b['cash']:,.2f}.")
    if ledger.unresolved:
        issues.append("Couldn't match these fund names to a ticker (add them to symbol_aliases in "
                      "config.json): " + "; ".join(ledger.unresolved))

    # --- allocation by asset type and by account (holdings plus cash, at Bank of Canada rates) ---
    alloc = {c: {"total": 0.0, "by_account": defaultdict(float)} for c in assets.ORDER}
    unknown = set()
    for h in holdings:
        parts = assets.split(h["symbol"], cfg["asset_classes"])
        if parts is None:
            unknown.add(h["symbol"])
            parts = {assets.OTHER: 1.0}
        for c, frac in parts.items():
            alloc.setdefault(c, {"total": 0.0, "by_account": defaultdict(float)})  # custom type from config
            alloc[c]["total"] += h["value_cad"] * frac
            alloc[c]["by_account"][h["number"]] += h["value_cad"] * frac
    for a in acct_out:
        alloc[assets.CASH]["total"] += a["cash"]
        alloc[assets.CASH]["by_account"][a["number"]] += a["cash"]
    if unknown:
        issues.append("Asset type unknown for " + ", ".join(sorted(unknown)) + "; counted as Other. "
                      "Set it in asset_classes in config.json.")
    by_type = [{"type": c, "value": v["total"], "by_account": dict(v["by_account"])}
               for c, v in alloc.items() if abs(v["total"]) >= 0.5]
    by_account = [{"number": a["number"], "type": a["type"],
                   "value": a["cash"] + sum(h["value_cad"] for h in holdings if h["number"] == a["number"])}
                  for a in acct_out]

    all_flows = sorted(f for n in nums for f in flows[n])
    total_value = sum(a["value"] for a in acct_out)
    total_in = sum(a["money_in"] for a in acct_out)

    # --- dividends ---
    years = list(range(int(first[:4]), year_now + 1))
    div_year = {n: defaultdict(float) for n in nums}
    div_symbol = defaultdict(float)
    lending = 0.0
    div_ttm = 0.0
    for a in acts:
        if a.kind == "dividend":
            c = cad(a.net, a.currency, a.date)
            div_year[a.account][int(a.date[:4])] += c
            div_symbol[a.symbol or "Unknown"] += c
            if a.date >= ttm_start:
                div_ttm += c
        elif a.kind == "interest":
            lending += cad(a.net, a.currency, a.date)

    # --- US withholding tax (dividends are reported after tax; estimate what was taken) ---
    wh = {n: {"usd": 0.0, "cad": 0.0, "ttm_usd": 0.0, "ttm_cad": 0.0, "payments": 0} for n in nums}
    wh_symbol = defaultdict(float)
    for a in acts:
        if a.kind == "dividend" and a.currency == "USD" and "NON-RES" in a.description.upper() and a.net > 0:
            w = a.net * US_WITHHOLDING / (1 - US_WITHHOLDING)
            row = wh[a.account]
            row["usd"] += w
            row["cad"] += cad(w, "USD", a.date)
            row["payments"] += 1
            wh_symbol[a.symbol or "Unknown"] += cad(w, "USD", a.date)
            if a.date >= ttm_start:
                row["ttm_usd"] += w
                row["ttm_cad"] += cad(w, "USD", a.date)
    us_div_exempt = sum(cad(a.net, a.currency, a.date) for a in acts if a.kind == "dividend"
                        and a.currency == "USD" and types[a.account] in TREATY_EXEMPT)

    # --- contributions ---
    contrib = {n: defaultdict(float) for n in nums}
    withdraw = {n: defaultdict(float) for n in nums}
    for a in acts:
        if a.kind == "contribution":
            contrib[a.account][int(a.date[:4])] += cad(a.net, a.currency, fx_date(a))
        elif a.kind == "withdrawal":
            withdraw[a.account][int(a.date[:4])] += -cad(a.net, a.currency, fx_date(a))

    fhsa = [fhsa_room(n, contrib[n], acts, year_now) for n in nums if types[n] == "FHSA"]
    tfsa_nums = [n for n in nums if types[n] == "TFSA"]
    tfsa = None
    cra = cfg["tfsa_room_from_cra"]
    if (cfg["tfsa_eligible_since"] or cra) and tfsa_nums:
        # Accepts a year (2014) or a date ("2014-01-05"); room is granted per calendar year.
        # Without an eligibility date, the table starts the year of the first deposit.
        first_deposit = min((y for n in tfsa_nums for y, v in contrib[n].items() if v), default=year_now)
        since = int(str(cfg["tfsa_eligible_since"]).strip()[:4]) if cfg["tfsa_eligible_since"] else first_deposit
        cra_by_year = cra_history(cra)
        anchor = max(cra_by_year.items()) if cra_by_year else None
        nonres = {int(y) for y in cfg["tfsa_nonresident_years"]}
        tfsa = tfsa_room(since, tfsa_nums, contrib, withdraw, year_now, cra_by_year, nonres)
        cra_nonres = {r["year"] for r in tfsa["rows"] if r["nonresident"] and r["year"] in cra_by_year}
        if len(cra_by_year) > 1 and cra_nonres != nonres:
            issues.append("TFSA: CRA gave no new room in " + (", ".join(map(str, sorted(cra_nonres))) or "no year")
                          + ", but tfsa_nonresident_years in config.json says "
                          + (", ".join(map(str, sorted(nonres))) or "none")
                          + ". CRA's records decide your room, so check which is right.")
        elif anchor and cfg["tfsa_eligible_since"]:
            # Cross-check CRA's figure against the user's own residency history.
            est = tfsa_room(since, tfsa_nums, contrib, withdraw, year_now, None, nonres)
            est_now = next(r["available"] for r in est["rows"] if r["year"] == anchor[0])
            if abs(est_now - anchor[1]) > 100:
                issues.append(
                    f"TFSA: CRA's room for {anchor[0]} is {anchor[1]:,.2f}, but your residency settings "
                    f"(eligible from {since}" + (f", non-resident {', '.join(map(str, sorted(nonres)))}" if nonres else "")
                    + f") give {est_now:,.2f}, a difference of {anchor[1] - est_now:,.2f}. "
                    "Worth confirming with CRA which years it counts.")
                tfsa["estimate"] = est

    return {
        "today": today,
        "last_sync": last_sync,
        "usdcad": fx_now,
        "issues": issues,
        "totals": {"value": total_value, "money_in": total_in, "gain": total_value - total_in,
                   "xirr": xirr(all_flows, total_value, today)},
        "accounts": acct_out,
        "series": {"dates": dates, "value": series_value, "money_in": series_in},
        "holdings": sorted(holdings, key=lambda h: -h["value_cad"]),
        "allocation": {"by_type": by_type, "by_account": by_account},
        "dividends": {
            "years": years,
            "by_account": {n: [round(div_year[n][y], 2) for y in years] for n in nums},
            "ttm": div_ttm,
            "lifetime": sum(sum(v.values()) for v in div_year.values()),
            "by_symbol": sorted(({"symbol": s, "cad": v} for s, v in div_symbol.items()), key=lambda r: -r["cad"]),
            "lending": lending,
        },
        "withholding": {
            "accounts": [{"number": n, "type": types[n], **wh[n]} for n in nums],
            "by_symbol": sorted(({"symbol": s, "cad": v} for s, v in wh_symbol.items()), key=lambda r: -r["cad"]),
            "exempt_us_dividends_cad": us_div_exempt,
        },
        "contributions": {
            "years": years,
            "by_account": {n: {"in": [round(contrib[n][y], 2) for y in years],
                               "out": [round(withdraw[n][y], 2) for y in years]} for n in nums},
        },
        "fhsa": fhsa,
        "tfsa": tfsa,
    }


def fhsa_room(number, contrib, acts, year_now):
    """FHSA room starts the year the account opens: $8,000 a year, up to $8,000 carried
    forward, $40,000 lifetime. Withdrawals don't restore room."""
    opened = min(int(a.date[:4]) for a in acts if a.account == number)
    rows, carry, used = [], 0.0, 0.0
    for y in range(opened, year_now + 1):
        room = min(FHSA_ANNUAL + carry, FHSA_LIFETIME - used)
        c = contrib.get(y, 0.0)
        left = room - c
        rows.append({"year": y, "room": room, "contributed": c, "left": left, "over": left < -0.005})
        used += c
        carry = min(FHSA_CARRY_MAX, max(0.0, left))
    return {"number": number, "opened": opened, "rows": rows,
            "lifetime_used": used, "lifetime_left": FHSA_LIFETIME - used}


def cra_history(cra):
    """{year: room on January 1} from config: either {"as_of": "2026-01-01", "room": 16418.60}
    or CRA's whole history as {"2026-01-01": 16418.60, "2025-01-01": 9418.60, ...}."""
    if not cra:
        return {}
    if "room" in cra:
        return {int(str(cra["as_of"])[:4]): float(cra["room"])}
    return {int(str(k)[:4]): float(v) for k, v in cra.items()}


def tfsa_limit(y, nonresident=()):
    """New room for year y; none for a year spent entirely as a non-resident."""
    return 0 if y in nonresident else TFSA_LIMITS.get(y, TFSA_LIMITS[max(TFSA_LIMITS)])


def tfsa_room(since, nums, contrib, withdraw, year_now, cra=None, nonresident=()):
    """TFSA room per year: carried in + that year's new room + last year's withdrawals, minus deposits.

    cra: {year: room on January 1} from CRA. Those figures are used as-is for their years, and
    the new room each implies shows how CRA treated the year (none means non-resident). Years
    before the earliest CRA figure are worked backwards from it; later years forwards. Without
    CRA figures, room starts at zero in `since` and `nonresident` years add no new room.
    """
    c = lambda y: sum(contrib[n].get(y, 0.0) for n in nums)
    w = lambda y: sum(withdraw[n].get(y, 0.0) for n in nums)
    lim = lambda y: tfsa_limit(y, nonresident)
    cra = dict(cra or {})
    first = max(2009, since)
    left_end = {first - 1: 0.0}  # room left at the end of each year
    if cra:
        a = min(cra)
        left_end = {a - 1: cra[a] - lim(a) - w(a - 1)}
        for y in range(a - 1, first - 1, -1):
            left_end[y - 1] = left_end[y] + c(y) - lim(y) - w(y - 1)
    rows = []
    for y in range(first, year_now + 1):
        carried, restored = left_end[y - 1], w(y - 1)
        if y in cra:
            available = cra[y]
            implied = available - carried - restored
            # Small gaps are exchange-rate rounding; a missing year's room means non-resident.
            limit = 0 if implied < tfsa_limit(y) / 2 else tfsa_limit(y)
        else:
            limit = lim(y)
            available = carried + limit + restored
        left_end[y] = available - c(y)
        rows.append({"year": y, "limit": limit, "nonresident": limit == 0, "carried": carried,
                     "restored": restored, "available": available, "contributed": c(y), "withdrawn": w(y),
                     "left": left_end[y], "over": left_end[y] < -0.005, "cra": cra.get(y)})
    return {"since": since, "anchored": bool(cra), "rows": rows, "left": left_end[year_now]}
