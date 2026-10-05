# Questrade → "Stock txns" sheet

`sheet_sync.py` adds new Questrade trades to a stock-transactions tab in a Google Sheet.
Set `sheet_id` (from the sheet URL) and `sheet_tab` in `config.json`.

- Reads the sheet's latest transaction date and fetches trades from then on.
- Fills A–H (type, account, broker, date, ticker, qty, currency, price); copies every
  formula and the formatting from the row above.
- Account is the type Questrade reports (`TFSA`, `RRSP`, `FHSA`), unless `sheet_account_labels`
  in `config.json` renames it for a holding (e.g. VOO in a TFSA → `tfsa-kid`).
- Trades on the sheet's last date are checked against what's already there, so nothing is
  added twice. A partial mismatch is reported, not written.
- Dividend reinvestments are skipped unless you pass `--include-reinvestments`.
- Shows the rows and asks before writing (`--yes` skips the question).

## One-time Google setup (service account, ~5 minutes)

1. Go to https://console.cloud.google.com, create a project (any name).
2. **APIs & Services → Library**: enable **Google Sheets API**.
3. **IAM & Admin → Service Accounts → Create service account** (any name, no roles needed).
4. Open it → **Keys → Add key → Create new key → JSON**. Save the file in this folder as
   `google_service_account.json`.
5. Copy the service account's email (looks like `name@project.iam.gserviceaccount.com`).
   In your sheet, **Share** it with that email as **Editor**.

That account can only open sheets you share with it. Keep the key file private.

## Run

```bash
cd ~/dev/questrade-export
.venv/bin/python sheet_sync.py
```

It uses the same `.latest_token` as the dashboard, so don't run both at the same moment.
