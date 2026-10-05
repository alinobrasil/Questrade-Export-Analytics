"""Paths and user settings."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = ROOT / ".latest_token"
DB_FILE = ROOT / "data" / "questrade.db"
CONFIG_FILE = ROOT / "config.json"

DEFAULTS = {
    # Accounts to ignore entirely, by account number.
    "skip_accounts": [],
    # Don't fetch activity before this date, unless overridden per account.
    "history_start": "2017-06-01",
    "history_start_by_account": {},
    # Fund name (as Questrade writes it in descriptions) -> ticker, for old rows with no ticker.
    "symbol_aliases": {},
    # Ticker -> asset type ("Equities", "Bonds", "Other") or a split like {"Equities": 0.8, "Bonds": 0.2},
    # for funds the app doesn't know.
    "asset_classes": {},
    # When you became eligible: 18+ and a Canadian resident (2009 at the earliest). A year or a date.
    # Enables TFSA room.
    "tfsa_eligible_since": None,
    # Your TFSA room from CRA My Account, e.g. {"as_of": "2026-01-01", "room": 16418.60}. Overrides the
    # eligibility-based estimate, since CRA knows your full residency and contribution history.
    "tfsa_room_from_cra": None,
    # Whole calendar years you were a non-resident for tax purposes: no TFSA room is added for them.
    "tfsa_nonresident_years": [],
    # sheet_sync.py: the Google Sheet to update, and holdings whose account label is renamed,
    # e.g. {"VOO": {"TFSA": "tfsa-kid", "RRSP": "RRSP Kid"}}.
    "sheet_id": "",
    "sheet_tab": "Stock txns",
    "sheet_account_labels": {},
}


def load():
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    return cfg
