"""Questrade API client with refresh-token rotation."""
import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

LOGIN_URL = "https://login.questrade.com/oauth2/token"
MAX_WINDOW = timedelta(days=30)  # API rejects ranges over 31 days; 30 leaves margin
CANDLE_CHUNK = timedelta(days=1500)  # API returns at most 2000 candles per request


class AuthError(Exception):
    pass


class Client:
    def __init__(self, token_file, token=None):
        """Exchange a refresh token for an access token.

        Refresh tokens are single use: the new one is saved to token_file before
        anything else can fail, so the chain of tokens is never broken.
        """
        source = "the token you entered"
        if token is None:
            if not token_file.exists():
                raise AuthError("No Questrade token saved yet. Paste one from the App Hub.")
            token, source = token_file.read_text().strip(), "the saved token"
        r = requests.get(LOGIN_URL, timeout=30,
                         params={"grant_type": "refresh_token", "refresh_token": token})
        if r.status_code != 200:
            raise AuthError(f"Questrade rejected {source} (HTTP {r.status_code}). It was already "
                            "used or is over 7 days old. Generate a new one in the App Hub.")
        d = r.json()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = token_file.with_name(token_file.name + ".tmp")
        tmp.write_text(d["refresh_token"])
        tmp.chmod(0o600)
        tmp.replace(token_file)
        self.base = d["api_server"].rstrip("/") + "/v1"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {d['access_token']}"

    def get(self, path, **params):
        for attempt in range(4):
            r = self.session.get(f"{self.base}/{path}", params=params, timeout=30)
            if r.status_code == 429:  # rate limited
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def accounts(self):
        return self.get("accounts")["accounts"]

    def balances(self, account):
        return self.get(f"accounts/{account}/balances")

    def positions(self, account):
        return self.get(f"accounts/{account}/positions")["positions"]

    def activities(self, account, since, progress=None):
        """All activities from `since` to now, walking back in 30-day windows."""
        end = datetime.now(timezone.utc)
        out, prev = [], Counter()
        while end > since:
            start = max(end - MAX_WINDOW, since)
            got = self.get(f"accounts/{account}/activities",
                           startTime=qt_time(start), endTime=qt_time(end))["activities"]
            # Questrade returns the boundary day in both adjacent windows. Drop rows the
            # previous window already returned, counting multiplicity so genuine identical
            # same-day transactions are kept.
            keys = [json.dumps(a, sort_keys=True) for a in got]
            seen, fresh = prev.copy(), []
            for a, k in zip(got, keys):
                if seen[k] > 0:
                    seen[k] -= 1
                else:
                    fresh.append(a)
            prev = Counter(keys)
            out += fresh
            if progress:
                progress(start, len(out))
            end = start
            time.sleep(0.05)
        return out

    def find_symbol_id(self, ticker):
        for s in self.get("symbols/search", prefix=ticker).get("symbols", []):
            if s["symbol"] == ticker:
                return s["symbolId"], s.get("currency")
        return None, None

    def candles(self, symbol_id, start, end):
        """Raw daily-candle response, with times in the format Questrade documents."""
        return self.get(f"markets/candles/{symbol_id}", interval="OneDay",
                        startTime=qt_time(start), endTime=qt_time(end))

    def daily_closes(self, symbol_id, start, end):
        """[(YYYY-MM-DD, close)] between start and end."""
        out = []
        while start < end:
            stop = min(start + CANDLE_CHUNK, end)
            out += [(c["start"][:10], c["close"]) for c in self.candles(symbol_id, start, stop).get("candles", [])]
            start = stop
        return out


def qt_time(dt):
    """2014-10-01T00:00:00-05:00: no microseconds."""
    return dt.replace(microsecond=0).isoformat()
