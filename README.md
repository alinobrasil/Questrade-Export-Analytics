# Questrade Insights

A local dashboard for your Questrade accounts: money put in vs. value over time,
returns, dividends, US tax withheld on dividends, and contribution room.
Everything runs on your computer. Your token and data never leave it.

## Run it

```bash
pip install requests
python3 -m qtinsights serve
```

Open http://127.0.0.1:8765. The first time, paste a refresh token (see below).
After that, press **Sync now**; the app keeps the token current on its own.

Command line equivalents:

```bash
python3 -m qtinsights sync           # pull new activity, balances, prices, exchange rates
python3 -m qtinsights sync TOKEN     # same, starting from a freshly generated token
python3 -m qtinsights check          # list data problems
python3 -m qtinsights import FILE    # load a questrade_export.json
```

## Getting a refresh token

1. Log in to Questrade's website and open **App Hub** (in your account menu, under
   *API centre* / *App Hub*). The first time, you'll be asked to activate API access.
2. Click **Register a personal app**. Give it any name, and turn on both permissions:
   - **Retrieve balances, positions, orders and executions** (account data)
   - **Retrieve delayed and real-time market data** (needed for price history; without
     it the value-over-time chart falls back to trade prices)
3. Under the app, click **New device** → **Generate new token**. Copy the token.
4. Paste it into the app's **Connect to Questrade** box, or run
   `python3 -m qtinsights sync TOKEN`.

A generated token must be used within **7 days**, and works **once**. You only need to
do this again if a sync says the token was rejected (see the appendix for why).

## Settings (`config.json`)

Copy `config.example.json` to `config.json` and edit it. `config.json` holds account
numbers and is ignored by git, along with your token, database and exports (see `.gitignore`).

| Key | Meaning |
|---|---|
| `skip_accounts` | Account numbers to ignore |
| `history_start` | Don't fetch activity before this date |
| `history_start_by_account` | Per-account override of `history_start` |
| `symbol_aliases` | Fund name → ticker, for old activity rows that have no ticker |
| `asset_classes` | Ticker → asset type, for funds the app doesn't recognise |
| `tfsa_room_from_cra` | Your TFSA room from CRA My Account: one figure, `{"as_of": "2026-01-01", "room": 12345.67}`, or the whole "contribution room history", `{"2026-01-01": 12345.67, "2025-01-01": …}`. Most accurate: the room table is worked out from the latest figure, and CRA's figure for each year is shown alongside |
| `tfsa_eligible_since` | When you were first 18+ and resident in Canada, as a year or date (e.g. `"2014-01-05"`, no earlier than 2009); enables TFSA room |
| `tfsa_nonresident_years` | Whole calendar years you were a non-resident for tax purposes; they add no TFSA room (e.g. `[2015, 2020]`) |
| `sheet_id`, `sheet_tab`, `sheet_account_labels` | Google Sheet settings for `sheet_sync.py` (see SHEETS.md) |

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

## Appendix: how the refresh token is stored

Questrade uses OAuth with **single-use refresh tokens**. Each time the app connects:

1. It sends the current refresh token to `login.questrade.com`.
2. Questrade replies with a short-lived **access token** (about 30 minutes, used for that
   sync's API calls and kept only in memory) and a **new refresh token**. The old refresh
   token stops working immediately.
3. The app saves the new refresh token **before making any other request**, so a failure
   later in the sync can't lose it. It writes to a temporary file
   (`.latest_token.tmp`) and then renames it over `.latest_token`, so the file is never
   left half-written, and sets its permissions to `600` (readable only by your user).

So the token lives in one place: **`.latest_token`** in the project folder, as plain text.
It is excluded from git. Anything that can read your user's files can read it, and with it
access your Questrade account data (and market data); it cannot place trades, because
personal apps aren't granted trading permission.

Things that break the chain, and then need a new token from App Hub:

- **More than 7 days without a sync.** An unused refresh token expires.
- **Using the token anywhere else**, e.g. pasting the same token into another tool, or
  running two programs that share `.latest_token` at the same moment (the dashboard and
  `sheet_sync.py` both use it, so run them one after the other). Whichever uses it first
  gets the new token; the other is left holding a dead one.
- **Revoking the app or device in App Hub.**

To disconnect entirely, delete `.latest_token` and revoke the device in App Hub.
