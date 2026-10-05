"""Turn raw Questrade activity rows into a clean, typed ledger."""
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

# Funds whose old activity rows carry only a description and that never appear with a
# ticker elsewhere. Users can add more in config.json "symbol_aliases".
BUILTIN_ALIASES = {
    "VANGUARD STAR FD VANGUARD TOTAL INTL STOCK INDEX FD": "VXUS",
}

# Where the fund name ends inside a Questrade description.
_NAME_END = re.compile(r"\s+(WE ACTED|REINV@|CASH DIV|UNIT DIST|DIST\b|JOURNAL POSITION|REC \d|"
                       r"AVG PRICE|ETF SHS|SUBST PAY|DISTRIBUTION|REVERSE SPLIT|CASH IN LIEU)")

# Registered accounts where US dividends are paid in full under the Canada-US tax treaty.
TREATY_EXEMPT = {"RRSP", "SRRSP", "LIRA", "LRSP", "RRIF", "LIF", "SRRIF", "PRIF", "RIF"}


def fund_name(description):
    s = " ".join(description.upper().split())
    m = _NAME_END.search(s)
    return (s[:m.start()] if m else s).strip()


def classify(row):
    t, a = row["type"], row["action"].strip()
    if t == "Deposits":
        return "contribution"
    if t == "Withdrawals":
        return "withdrawal"
    if t == "Transfers":
        return "transfer"
    if t == "Dividend reinvestment" or a == "REI":
        return "reinvest"
    if t == "Dividends":
        return "dividend"
    if t == "Trades":
        return "trade"
    if t == "FX conversion":
        return "fx"
    if t == "Interest" or a == "LFJ":
        return "interest"
    if t == "Fees and rebates":
        return "fee"
    if t == "Corporate actions":
        return "corporate"
    if a == "BRW":
        return "journal"
    return "other"


@dataclass
class Act:
    account: str
    acct_type: str
    date: str  # YYYY-MM-DD
    kind: str
    symbol: str  # "" when not tied to a security or not resolvable
    currency: str
    qty: float
    price: float
    net: float
    description: str


class SymbolResolver:
    """Fills in tickers for rows that only carry a fund name."""

    def __init__(self, rows, aliases):
        self.aliases = {fund_name(k): v for k, v in {**BUILTIN_ALIASES, **aliases}.items()}
        learned = defaultdict(Counter)
        for r in rows:
            if r["symbol"] and r["description"].strip():
                learned[fund_name(r["description"])][r["symbol"]] += 1
        # Names that map to several tickers (e.g. DLR.TO and DLR.U.TO) can't be used.
        self.learned = {n: c.most_common(1)[0][0] for n, c in learned.items() if len(c) == 1}
        self.unresolved = Counter()

    def resolve(self, row):
        if row["symbol"]:
            return row["symbol"]
        name = fund_name(row["description"])
        if name in self.aliases:
            return self.aliases[name]
        if name in self.learned:
            return self.learned[name]
        sym = self._fuzzy(name)
        if sym:
            return sym
        if row["quantity"] or row["type"] == "Dividends":
            self.unresolved[name] += 1
        return ""

    def _fuzzy(self, name):
        """Match renamed funds, e.g. 'VANGUARD TOTAL STOCK MARKET ETF' after 'MORNINGSTAR' was added."""
        words = set(name.split())
        scored = []
        for known, sym in self.learned.items():
            k = set(known.split())
            score = len(words & k) / len(words | k)
            if score >= 0.8:
                scored.append((score, sym))
        syms = {s for _, s in scored}
        return scored and len(syms) == 1 and syms.pop()


def unit_price(row):
    """Per-share price; reinvestments report price 0 but carry the amount and quantity."""
    if row["price"]:
        return row["price"]
    if row["quantity"]:
        return abs(row["grossAmount"] or row["netAmount"]) / abs(row["quantity"])
    return 0.0


class Ledger:
    def __init__(self, accounts, rows_by_account, aliases):
        all_rows = [r for rows in rows_by_account.values() for r in rows]
        self.resolver = SymbolResolver(all_rows, aliases)
        types = {a["number"]: a["type"] for a in accounts}
        self.acts = []
        self.symbol_currency = {}
        for number, rows in rows_by_account.items():
            for r in rows:
                sym = self.resolver.resolve(r)
                act = Act(number, types[number], r["tradeDate"][:10], classify(r), sym, r["currency"],
                          r["quantity"] or 0.0, unit_price(r), r["netAmount"] or 0.0, r["description"])
                self.acts.append(act)
                if sym and act.qty and act.kind in ("trade", "reinvest", "journal"):
                    self.symbol_currency[sym] = act.currency
        self.acts.sort(key=lambda a: a.date)

    @property
    def unresolved(self):
        return dict(self.resolver.unresolved)

    def held_symbols(self):
        """{symbol: (first date held, last date held or None if still held)}"""
        qty, first, last = defaultdict(float), {}, {}
        for a in self.acts:
            if not (a.symbol and a.qty):
                continue
            key = (a.account, a.symbol)
            qty[key] += a.qty
            first.setdefault(a.symbol, a.date)
            last[a.symbol] = a.date
        still = {s for (_, s), q in qty.items() if abs(q) > 1e-6}
        return {s: (first[s], None if s in still else last[s]) for s in first}

    def walk(self, dates):
        """Yield (date, positions, cash) after applying every activity on or before each date.

        positions: {account: {symbol: qty}}, cash: {account: {currency: amount}}
        Also tracks the last traded price per symbol, as a fallback when no price history exists.
        """
        pos = defaultdict(lambda: defaultdict(float))
        cash = defaultdict(lambda: defaultdict(float))
        self.last_trade_price = {}
        i = 0
        for d in dates:
            while i < len(self.acts) and self.acts[i].date <= d:
                a = self.acts[i]
                cash[a.account][a.currency] += a.net
                if a.symbol and a.qty:
                    pos[a.account][a.symbol] += a.qty
                    if a.price:
                        self.last_trade_price[a.symbol] = a.price
                i += 1
            yield d, pos, cash
