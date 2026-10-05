"""Local SQLite store. Raw API rows are kept as JSON; analytics interprets them."""
import json
import sqlite3
from bisect import bisect_right

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(number TEXT PRIMARY KEY, type TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS activities(account TEXT, trade_date TEXT, raw TEXT);
CREATE INDEX IF NOT EXISTS activities_by_date ON activities(account, trade_date);
CREATE TABLE IF NOT EXISTS snapshots(account TEXT PRIMARY KEY, balances TEXT, positions TEXT, taken_at TEXT);
CREATE TABLE IF NOT EXISTS fx(date TEXT PRIMARY KEY, usdcad REAL);
CREATE TABLE IF NOT EXISTS symbols(symbol TEXT PRIMARY KEY, symbol_id INTEGER, currency TEXT);
CREATE TABLE IF NOT EXISTS prices(symbol TEXT, date TEXT, close REAL, PRIMARY KEY(symbol, date));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)

    def commit(self):
        self.db.commit()

    # --- accounts and activities ---
    def save_account(self, acct):
        self.db.execute("INSERT OR REPLACE INTO accounts VALUES (?,?,?)",
                        (acct["number"], acct["type"], json.dumps(acct)))

    def accounts(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT raw FROM accounts ORDER BY number")]

    def last_activity_date(self, account):
        return self.db.execute("SELECT max(trade_date) FROM activities WHERE account=?",
                               (account,)).fetchone()[0]

    def replace_activities(self, account, since_date, rows):
        """Replace every activity dated since_date or later with rows (also from since_date on)."""
        self.db.execute("DELETE FROM activities WHERE account=? AND trade_date>=?", (account, since_date))
        self.db.executemany("INSERT INTO activities VALUES (?,?,?)",
                            [(account, r["tradeDate"][:10], json.dumps(r))
                             for r in rows if r["tradeDate"][:10] >= since_date])

    def activities(self, account):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT raw FROM activities WHERE account=? ORDER BY trade_date, rowid", (account,))]

    # --- current balances and positions ---
    def save_snapshot(self, account, balances, positions, taken_at):
        self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)",
                        (account, json.dumps(balances), json.dumps(positions), taken_at))

    def snapshot(self, account):
        r = self.db.execute("SELECT balances, positions, taken_at FROM snapshots WHERE account=?",
                            (account,)).fetchone()
        return (json.loads(r[0]), json.loads(r[1]), r[2]) if r else (None, None, None)

    # --- exchange rates ---
    def save_fx(self, rows):
        self.db.executemany("INSERT OR REPLACE INTO fx VALUES (?,?)", rows)

    def last_fx_date(self):
        return self.db.execute("SELECT max(date) FROM fx").fetchone()[0]

    def fx_table(self):
        return SeriesLookup(self.db.execute("SELECT date, usdcad FROM fx ORDER BY date").fetchall())

    # --- symbols and prices ---
    def save_symbol(self, symbol, symbol_id, currency):
        self.db.execute("INSERT OR REPLACE INTO symbols VALUES (?,?,?)", (symbol, symbol_id, currency))

    def symbols(self):
        return {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT symbol, symbol_id, currency FROM symbols")}

    def save_prices(self, symbol, rows):
        self.db.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?)", [(symbol, d, c) for d, c in rows])

    def last_price_date(self, symbol):
        return self.db.execute("SELECT max(date) FROM prices WHERE symbol=?", (symbol,)).fetchone()[0]

    def price_tables(self):
        out = {}
        for sym, d, c in self.db.execute("SELECT symbol, date, close FROM prices ORDER BY symbol, date"):
            out.setdefault(sym, []).append((d, c))
        return {s: SeriesLookup(rows) for s, rows in out.items()}

    # --- misc ---
    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def meta(self, key):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None


class SeriesLookup:
    """Dated values; at(date) returns the latest value on or before date."""

    def __init__(self, rows):
        self.dates = [d for d, _ in rows]
        self.values = [v for _, v in rows]

    def __bool__(self):
        return bool(self.dates)

    def at(self, date):
        i = bisect_right(self.dates, date) - 1
        if i >= 0:
            return self.values[i]
        return self.values[0] if self.values else None  # before the first value: use the earliest
