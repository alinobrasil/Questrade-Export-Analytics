"""Pull accounts, activities, balances, prices and exchange rates into the local store."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from . import config
from .ledger import Ledger
from .questrade import Client

# Re-fetch this much recent history on every sync, to pick up late-settling activity.
OVERLAP = timedelta(days=14)
BOC_URL = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"


def _utc(d):
    return datetime.fromisoformat(d).replace(tzinfo=timezone.utc)


def sync(store, token=None, log=print):
    cfg = config.load()
    client = Client(config.TOKEN_FILE, token)
    skip = set(cfg["skip_accounts"])
    for acct in client.accounts():
        n = acct["number"]
        if n in skip:
            log(f"Skipping account {n} ({acct['type']})")
            continue
        store.save_account(acct)
        start = cfg["history_start_by_account"].get(n, cfg["history_start"])
        last = store.last_activity_date(n)
        since = max(_utc(last) - OVERLAP, _utc(start)) if last else _utc(start)
        log(f"Account {n} ({acct['type']}): activity since {since:%Y-%m-%d}")
        rows = client.activities(n, since, progress=lambda s, total: log(f"  {s:%Y-%m}: {total} rows", end="\r"))
        log("")
        store.replace_activities(n, since.strftime("%Y-%m-%d"), rows)
        store.save_snapshot(n, client.balances(n), client.positions(n),
                            datetime.now(timezone.utc).isoformat(timespec="seconds"))
        store.commit()
    sync_fx(store, log)
    sync_prices(store, client, cfg, log)
    store.set_meta("last_sync", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    store.commit()
    log("Sync complete.")


def sync_fx(store, log=print):
    """Bank of Canada daily USD/CAD rates."""
    start = store.last_fx_date() or "2009-01-01"
    r = requests.get(BOC_URL, params={"start_date": start}, timeout=30)
    r.raise_for_status()
    rows = [(o["d"], float(o["FXUSDCAD"]["v"])) for o in r.json()["observations"] if o.get("FXUSDCAD")]
    store.save_fx(rows)
    store.commit()
    log(f"Exchange rates: {len(rows)} days loaded")


def sync_prices(store, client, cfg, log=print):
    """Daily closes for every symbol ever held, from when it was first held."""
    accounts = store.accounts()
    ledger = Ledger(accounts, {a["number"]: store.activities(a["number"]) for a in accounts},
                    cfg["symbol_aliases"])
    known = store.symbols()
    today = datetime.now(timezone.utc)
    for sym, (first, last_held) in sorted(ledger.held_symbols().items()):
        if sym not in known:
            sid, cur = client.find_symbol_id(sym)
            if not sid:
                log(f"  {sym}: not found on Questrade, using trade prices instead")
                continue
            store.save_symbol(sym, sid, cur or ledger.symbol_currency.get(sym))
            known[sym] = (sid, cur)
        sid = known[sym][0]
        have = store.last_price_date(sym)
        start = _utc(have) + timedelta(days=1) if have else _utc(first) - timedelta(days=7)
        end = min(today, _utc(last_held) + timedelta(days=7)) if last_held else today
        if start >= end:
            continue
        try:
            rows = client.daily_closes(sid, start, end)
        except requests.HTTPError as e:
            if e.response.status_code == 403:  # app lacks the market data permission; no symbol will work
                store.set_meta("price_error", "scope")
                store.commit()
                log("  Prices: your Questrade app doesn't have market data permission. In App Hub, turn on "
                    "'Retrieve delayed and real-time market data', then generate and paste a new token.")
                return
            log(f"  {sym}: price history unavailable ({e.response.status_code}), using trade prices")
            continue
        store.set_meta("price_error", "")
        if not rows:
            log(f"  {sym}: Questrade returned no prices for {start:%Y-%m-%d} to {end:%Y-%m-%d}. "
                f"Run 'python3 -m qtinsights debug-prices {sym}' and share the output.")
            continue
        store.save_prices(sym, rows)
        store.commit()
        log(f"  {sym}: {len(rows)} daily prices")


def import_export(store, path, log=print):
    """Load a questrade_export.json written by the original export script.

    That script downloaded window-boundary days twice, so exact repeats are dropped here.
    """
    data = json.loads(Path(path).read_text())
    cfg = config.load()
    for n, d in data.items():
        if n in cfg["skip_accounts"]:
            continue
        store.save_account(d["account"])
        rows, seen = [], set()
        for r in d["activities"]:
            k = json.dumps(r, sort_keys=True)
            if k not in seen:
                seen.add(k)
                rows.append(r)
        store.replace_activities(n, "0000-00-00", rows)
        store.save_snapshot(n, d["balances"], d["positions"],
                            datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc).isoformat(timespec="seconds"))
        log(f"Imported {len(rows)} activities for {n} ({d['account']['type']}), "
            f"dropped {len(d['activities']) - len(rows)} repeats")
    store.commit()
