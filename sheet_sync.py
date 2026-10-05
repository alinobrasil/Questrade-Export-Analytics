#!/usr/bin/env python3
"""Add new Questrade trades to a Google Sheet tab of stock transactions (see SHEETS.md).

Fetches trades since the latest date in the sheet and appends them below the last row,
filling columns A-H from Questrade and extending every formula from the row above.

Usage:
  .venv/bin/python sheet_sync.py                         preview, then ask before writing
  .venv/bin/python sheet_sync.py --yes                   write without asking
  .venv/bin/python sheet_sync.py --include-reinvestments also add dividend reinvestments

Uses the same .latest_token as the dashboard, so don't run both at the same time.
"""
import argparse
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from qtinsights import config
from qtinsights.questrade import AuthError, Client

CFG = config.load()
SHEET_ID = CFG["sheet_id"]
TAB = CFG["sheet_tab"]
DATA_COLS = 8  # A-H come from Questrade; everything after is extended from the row above
BROKER = "questrade"

# Holdings whose account label is renamed, e.g. {"VOO": {"TFSA": "tfsa-kid"}} (config "sheet_account_labels").
ACCOUNT_LABELS = CFG["sheet_account_labels"]

# Google sign-in: a service account key (recommended), or an OAuth desktop client. See SHEETS.md.
GOOGLE_SERVICE_ACCOUNT = config.ROOT / "google_service_account.json"
GOOGLE_CREDENTIALS = config.ROOT / "google_credentials.json"
GOOGLE_TOKEN = config.ROOT / "data" / "google_token.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ---------- mapping Questrade rows to sheet rows ----------

def sheet_symbol(symbol):
    """VFV.TO -> TSE:VFV (GOOGLEFINANCE style); US tickers unchanged."""
    if symbol.endswith(".TO"):
        return "TSE:" + symbol[:-3]
    if symbol.endswith(".VN"):
        return "CVE:" + symbol[:-3]
    return symbol


def account_label(acct_type, symbol):
    return ACCOUNT_LABELS.get(symbol, {}).get(acct_type, acct_type)


def base_account(label):
    """'RRSP Kid' -> RRSP, 'tfsa-kid' -> TFSA, so labels can be compared across naming styles."""
    return re.split(r"[\s-]", str(label).strip())[0].upper()


def to_sheet_row(acct_type, act):
    qty = act["quantity"]
    price = act["price"] or (abs(act["netAmount"]) / abs(qty) if qty else 0)  # reinvestments report price 0
    kind = "Buy" if qty > 0 else "Sell"
    d = act["tradeDate"][:10]
    return [kind, account_label(acct_type, act["symbol"]), BROKER, d, sheet_symbol(act["symbol"]),
            qty, act["currency"], round(price, 6)]


# ---------- reading the sheet ----------

def parse_date(v):
    """Sheet dates arrive as serial numbers; a few old rows are text like '9/11/2026'."""
    if isinstance(v, str) and re.fullmatch(r"\d+(\.\d+)?", v.strip()):
        v = float(v)
    if isinstance(v, (int, float)) and v > 0:
        return (date(1899, 12, 30) + timedelta(days=int(v))).isoformat()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(v).strip())
    if m:
        return date(int(m[3]), int(m[1]), int(m[2])).isoformat()
    return None


def last_row(rows):
    """1-based index of the last row with a date or ticker."""
    for i in range(len(rows) - 1, 0, -1):
        r = rows[i] + [""] * 5
        if str(r[3]).strip() or str(r[4]).strip():
            return i + 1
    return 1


# A cell reference like D172 or $D172, not part of a name (LOG10), a function call or a sheet name.
_REF = re.compile(r"(?<![A-Za-z0-9_.])(\$?)([A-Z]{1,3})(\$?)(\d+)(?![\d(A-Za-z_!])")


def shift_formula(formula, delta):
    """Move relative row references by delta, like dragging a formula down. Leaves $-rows,
    other sheets' absolute refs and text inside quotes alone."""
    parts = formula.split('"')
    for i in range(0, len(parts), 2):  # even parts are outside quotes
        parts[i] = _REF.sub(lambda m: m[0] if m[3] else f"{m[1]}{m[2]}{m[3]}{int(m[4]) + delta}", parts[i])
    return '"'.join(parts)


# ---------- deciding what to add ----------

def plan(sheet_rows, trades):
    """sheet_rows: values (formulas rendered) incl. header. trades: [(acct_type, activity)].
    Returns (new rows as A-H lists, warnings)."""
    last = last_row(sheet_rows)
    dates = [parse_date((r + [""] * 4)[3]) for r in sheet_rows[1:last]]
    last_date = max(d for d in dates if d)

    # What the sheet already has on its last date, by (symbol, account), to avoid duplicates.
    have = defaultdict(float)
    for r, d in zip(sheet_rows[1:last], dates):
        if d == last_date:
            r = r + [""] * DATA_COLS
            have[(str(r[4]).strip(), base_account(r[1]))] += float(r[5] or 0)

    new, warnings = [], []
    same_day = defaultdict(list)
    for acct_type, act in trades:
        row = to_sheet_row(acct_type, act)
        if row[3] > last_date:
            new.append(row)
        elif row[3] == last_date:
            same_day[(row[4], base_account(row[1]))].append(row)
    for key, rows in same_day.items():
        got = sum(r[5] for r in rows)
        if abs(have.get(key, 0.0)) < 1e-9:
            new += rows  # missing from the sheet entirely
        elif abs(have[key] - got) > 1e-4:
            warnings.append(f"{last_date} {key[1]} {key[0]}: sheet has {have[key]:g} shares, Questrade "
                            f"has {got:g}. Not added; please check by hand.")
    new.sort(key=lambda r: (r[3], r[4]))
    return last, last_date, new, warnings


# ---------- Questrade ----------

def fetch_trades(since_date, include_reinvestments):
    try:
        client = Client(config.TOKEN_FILE)
    except AuthError as e:
        sys.exit(str(e))
    since = datetime.fromisoformat(since_date).replace(tzinfo=timezone.utc) - timedelta(days=1)
    out = []
    for acct in client.accounts():
        if acct["number"] in CFG["skip_accounts"]:
            continue
        for a in client.activities(acct["number"], since):
            is_trade = a["type"] == "Trades" and a["action"] in ("Buy", "Sell")
            is_reinvest = include_reinvestments and a["type"] == "Dividend reinvestment"
            if (is_trade or is_reinvest) and a["symbol"] and a["quantity"] and a["tradeDate"][:10] >= since_date:
                out.append((acct["type"], a))
    return out


# ---------- writing ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="write without asking")
    ap.add_argument("--include-reinvestments", action="store_true", help="also add dividend reinvestments")
    args = ap.parse_args()

    if not SHEET_ID:
        sys.exit("Set sheet_id in config.json to the ID from your Google Sheet's URL.")
    import gspread
    from gspread.utils import ValueRenderOption
    if GOOGLE_SERVICE_ACCOUNT.exists():
        gc = gspread.service_account(filename=str(GOOGLE_SERVICE_ACCOUNT), scopes=SCOPES)
    elif GOOGLE_CREDENTIALS.exists():
        gc = gspread.oauth(scopes=SCOPES, credentials_filename=str(GOOGLE_CREDENTIALS),
                           authorized_user_filename=str(GOOGLE_TOKEN))
    else:
        sys.exit(f"No Google sign-in set up. Save {GOOGLE_SERVICE_ACCOUNT.name} here; see SHEETS.md.")
    ws = gc.open_by_key(SHEET_ID).worksheet(TAB)
    rows = ws.get_values("A:AD", value_render_option=ValueRenderOption.formula)

    last, last_date, _, _ = plan(rows, [])
    print(f"Sheet's last transaction: {last_date} (row {last}). Fetching Questrade trades since then…")
    last, last_date, new, warnings = plan(rows, fetch_trades(last_date, args.include_reinvestments))
    for w in warnings:
        print("WARNING:", w)
    if not new:
        print("Nothing new to add.")
        return

    template = rows[last - 1] + [""] * (30 - len(rows[last - 1]))
    out = []
    for i, data in enumerate(new):
        r = last + 1 + i
        extended = [shift_formula(v, r - last) if isinstance(v, str) and v.startswith("=") else ""
                    for v in template[DATA_COLS:]]
        out.append(data + extended)
    width = max(len(v) for v in out)
    while width > DATA_COLS and all(not row[width - 1] for row in out):  # trim empty trailing columns
        width -= 1
    out = [row[:width] for row in out]

    print(f"\nWill add {len(out)} row(s) starting at row {last + 1}:")
    for row in out:
        print("  " + " | ".join(str(v) for v in row[:DATA_COLS]))
    if not args.yes and input("\nWrite these to the sheet? [y/N] ").strip().lower() != "y":
        print("Nothing written.")
        return

    if last + len(out) > ws.row_count:
        ws.add_rows(last + len(out) - ws.row_count)
    end_col = gspread.utils.rowcol_to_a1(1, width).rstrip("1")
    ws.update(values=out, range_name=f"A{last + 1}:{end_col}{last + len(out)}", value_input_option="USER_ENTERED")
    # Same formatting as the row above (date format, number formats, alignment).
    ws.spreadsheet.batch_update({"requests": [{"copyPaste": {
        "source": {"sheetId": ws.id, "startRowIndex": last - 1, "endRowIndex": last,
                   "startColumnIndex": 0, "endColumnIndex": 30},
        "destination": {"sheetId": ws.id, "startRowIndex": last, "endRowIndex": last + len(out),
                        "startColumnIndex": 0, "endColumnIndex": 30},
        "pasteType": "PASTE_FORMAT"}}]})
    print(f"Added {len(out)} row(s).")


if __name__ == "__main__":
    main()
