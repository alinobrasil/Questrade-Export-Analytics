import json
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from qtinsights import analytics, config
from qtinsights.analytics import fhsa_room, tfsa_room, xirr
from qtinsights.ledger import Act, Ledger, fund_name
from qtinsights.questrade import Client
from qtinsights.store import Store
from qtinsights.sync import import_export

ROOT = Path(__file__).resolve().parent.parent


def row(date, type_, action="", symbol="", qty=0.0, price=0.0, net=0.0, currency="CAD", desc="", gross=0.0):
    return {"tradeDate": f"{date}T00:00:00.000000-05:00", "type": type_, "action": action, "symbol": symbol,
            "symbolId": 0, "quantity": qty, "price": price, "netAmount": net, "grossAmount": gross,
            "commission": 0, "currency": currency, "description": desc}


ACCTS = [{"number": "1", "type": "TFSA"}]


class LedgerTest(unittest.TestCase):
    def test_fund_name_strips_trade_details(self):
        self.assertEqual(fund_name("VANGUARD S&P 500 ETF       WE ACTED AS AGENT"), "VANGUARD S&P 500 ETF")
        self.assertEqual(fund_name("VANGUARD SHORT TERM BOND ETF REINV@U$78.13 REC 04/01/26"),
                         "VANGUARD SHORT TERM BOND ETF")

    def test_nameless_rows_get_tickers(self):
        rows = [
            row("2020-01-02", "Trades", "Buy", "VTI", 5, 150, -750, "USD",
                "VANGUARD INDEX FUNDS VANGUARD MORNINGSTAR TOTAL STOCK MARKET ETF WE ACTED AS AGENT"),
            # older row, old fund name, no ticker: matched by name similarity
            row("2019-01-02", "Trades", "Buy", "", 10, 140, -1400, "USD",
                "VANGUARD INDEX FUNDS VANGUARD TOTAL STOCK MARKET ETF WE ACTED AS AGENT"),
            # built-in alias
            row("2019-01-03", "Trades", "Buy", "", 3, 50, -150, "USD",
                "VANGUARD STAR FD VANGUARD TOTAL INTL STOCK INDEX FD ETF SHS WE ACTED AS AGENT"),
            # user alias from config
            row("2019-01-04", "Trades", "Buy", "", 1, 10, -10, "CAD", "SOME OLD FUND WE ACTED AS AGENT"),
        ]
        led = Ledger(ACCTS, {"1": rows}, {"SOME OLD FUND": "OLD.TO"})
        self.assertEqual([a.symbol for a in led.acts], ["VTI", "VXUS", "OLD.TO", "VTI"])
        self.assertEqual(led.unresolved, {})

    def test_unmatched_names_are_reported(self):
        led = Ledger(ACCTS, {"1": [row("2019-01-02", "Trades", "Buy", "", 1, 1, -1, desc="MYSTERY FUND WE ACTED")]}, {})
        self.assertEqual(led.unresolved, {"MYSTERY FUND": 1})

    def test_ambiguous_names_are_not_guessed(self):
        # Norbert's gambit journals use one fund name for two tickers.
        rows = [row("2021-01-01", "Other", "BRW", "DLR.TO", -10, desc="GLOBAL X US DLR CURRENCY ETF JOURNAL POSITION TO USD"),
                row("2021-01-01", "Other", "BRW", "DLR.U.TO", 10, currency="USD", desc="GLOBAL X US DLR CURRENCY ETF JOURNAL POSITION FROM CAD")]
        led = Ledger(ACCTS, {"1": rows}, {})
        self.assertNotIn("GLOBAL X US DLR CURRENCY ETF", led.resolver.learned)

    def test_walk_rebuilds_positions_and_cash(self):
        rows = [row("2020-01-01", "Deposits", "CON", net=1000),
                row("2020-01-02", "Trades", "Buy", "XEQT.TO", 30, 30, -900),
                row("2020-02-01", "Dividend reinvestment", "REI", "XEQT.TO", 1, 0, -31, gross=-31),
                row("2020-02-01", "Dividends", "", "XEQT.TO", net=31),
                row("2020-03-01", "Trades", "Sell", "XEQT.TO", -10, 35, 350)]
        led = Ledger(ACCTS, {"1": rows}, {})
        snaps = {d: (dict(p["1"]), dict(c["1"])) for d, p, c in led.walk(["2020-01-31", "2020-12-31"])}
        self.assertEqual(snaps["2020-01-31"], ({"XEQT.TO": 30}, {"CAD": 100}))
        self.assertEqual(snaps["2020-12-31"], ({"XEQT.TO": 21}, {"CAD": 450}))
        self.assertEqual(led.last_trade_price["XEQT.TO"], 35)

    def test_reinvestment_price_comes_from_amount(self):
        led = Ledger(ACCTS, {"1": [row("2020-02-01", "Dividend reinvestment", "REI", "BSV", 1, 0, -78.64, "USD", gross=0)]}, {})
        self.assertAlmostEqual(led.acts[0].price, 78.64)


class RoomTest(unittest.TestCase):
    def acts(self, *dates):
        return [Act("9", "FHSA", d, "contribution", "", "CAD", 0, 0, 0, "") for d in dates]

    def test_fhsa_carry_forward_and_lifetime(self):
        r = fhsa_room("9", {2024: 250, 2025: 15750, 2026: 7581.2}, self.acts("2024-12-09"), 2026)
        self.assertEqual([x["room"] for x in r["rows"]], [8000, 15750, 8000])
        self.assertAlmostEqual(r["rows"][-1]["left"], 418.8)
        self.assertAlmostEqual(r["lifetime_left"], 40000 - 23581.2)

    def test_fhsa_carry_forward_is_capped(self):
        r = fhsa_room("9", {2023: 0, 2024: 0}, self.acts("2023-05-01"), 2024)
        self.assertEqual(r["rows"][1]["room"], 16000)  # 8,000 + at most 8,000 carried

    def test_fhsa_over_contribution_flagged(self):
        r = fhsa_room("9", {2024: 9000}, self.acts("2024-01-05"), 2024)
        self.assertTrue(r["rows"][0]["over"])

    def test_tfsa_withdrawal_returns_next_year(self):
        contrib = {"1": defaultdict(float, {2022: 81500})}
        withdraw = {"1": defaultdict(float, {2022: 8300})}
        r = tfsa_room(2009, ["1"], contrib, withdraw, 2023)
        by_year = {x["year"]: x["left"] for x in r["rows"]}
        self.assertEqual(by_year[2022], 0)  # 81,500 cumulative limit 2009-2022, all used
        self.assertEqual(by_year[2023], 6500 + 8300)


class TfsaAnchorTest(unittest.TestCase):
    def test_cra_figure_anchors_every_year(self):
        contrib = {"1": defaultdict(float, {2021: 12000, 2022: 6000, 2024: 15000})}
        withdraw = {"1": defaultdict(float, {2022: 8300})}
        r = tfsa_room(2021, ["1"], contrib, withdraw, 2026, cra={2026: 16418.60})
        rows = {x["year"]: x for x in r["rows"]}
        self.assertAlmostEqual(rows[2026]["available"], 16418.60)  # matches CRA on Jan 1
        self.assertAlmostEqual(rows[2025]["left"], 16418.60 - 7000)
        self.assertEqual(rows[2023]["restored"], 8300)
        for y in range(2021, 2026):  # each year's carried-in is the previous year's left
            self.assertAlmostEqual(rows[y + 1]["carried"], rows[y]["left"])


class TfsaNonresidentTest(unittest.TestCase):
    def test_nonresident_years_add_no_room(self):
        none = defaultdict(float)
        r = tfsa_room(2014, ["1"], {"1": none}, {"1": none}, 2016, nonresident={2015})
        self.assertEqual([x["limit"] for x in r["rows"]], [5500, 0, 5500])
        self.assertEqual(r["left"], 11000)


class TfsaCraYearsTest(unittest.TestCase):
    def test_cra_figures_used_per_year_and_missing_room_marks_nonresident(self):
        none = defaultdict(float)
        # CRA: 2014 room 5,500; 2015 added 10,000; 2016 added nothing (non-resident).
        r = tfsa_room(2014, ["1"], {"1": none}, {"1": none}, 2017,
                      cra={2014: 5500.0, 2015: 15500.0, 2016: 15500.0})
        rows = {x["year"]: x for x in r["rows"]}
        self.assertEqual([rows[y]["limit"] for y in (2014, 2015, 2016, 2017)], [5500, 10000, 0, 5500])
        self.assertTrue(rows[2016]["nonresident"])
        self.assertEqual(rows[2017]["available"], 21000)


class CraHistoryTest(unittest.TestCase):
    def test_both_config_forms(self):
        self.assertEqual(analytics.cra_history({"as_of": "2026-01-01", "room": 16418.6}), {2026: 16418.6})
        self.assertEqual(analytics.cra_history({"2026-01-01": 16418.6, "2024-01-01": 17591.92}),
                         {2026: 16418.6, 2024: 17591.92})
        self.assertEqual(analytics.cra_history(None), {})


class FxDateTest(unittest.TestCase):
    def test_cancellation_uses_original_date(self):
        act = Act("1", "TFSA", "2019-02-22", "contribution", "", "USD", 0, 0, -5000,
                  "AS OF 02/21/19 CANCELLATION PAD FAIL 5,000.00 U$ CNV@ 1.32330000")
        self.assertEqual(analytics.fx_date(act), "2019-02-21")
        self.assertEqual(analytics.fx_date(Act("1", "TFSA", "2019-02-21", "contribution", "", "USD", 0, 0,
                                               5000, "PAD USD TFSA DEP")), "2019-02-21")


class ReturnTest(unittest.TestCase):
    def test_xirr_one_year(self):
        self.assertAlmostEqual(xirr([("2020-01-01", 1000)], 1100, "2020-12-31"), 0.10, places=2)

    def test_xirr_no_flows(self):
        self.assertIsNone(xirr([], 0, "2020-01-01"))


class ClientTest(unittest.TestCase):
    def test_boundary_days_are_not_downloaded_twice(self):
        """Questrade includes the boundary day in both adjacent windows."""
        rows = [{"tradeDate": f"2026-{m:02d}-{d:02d}T00:00:00-04:00", "n": i}
                for i, (m, d) in enumerate([(9, 1), (9, 4), (9, 4), (8, 20), (8, 5), (7, 1)])]
        c = Client.__new__(Client)

        def get(path, startTime, endTime):
            s, e = startTime[:10], endTime[:10]
            return {"activities": [r for r in rows if s <= r["tradeDate"][:10] <= e]}
        c.get = get
        now = datetime(2026, 10, 4, 7, tzinfo=timezone.utc)
        with mock.patch("qtinsights.questrade.datetime") as dt, mock.patch("qtinsights.questrade.time.sleep"):
            dt.now.return_value = now
            got = c.activities("1", now - timedelta(days=120))
        self.assertEqual(sorted(r["n"] for r in got), [0, 1, 2, 3, 4, 5])


@unittest.skipUnless((ROOT / "questrade_export.json").exists(), "needs a real export")
class RealDataTest(unittest.TestCase):
    """The rebuilt ledger must agree with Questrade's own positions and cash."""

    def test_ledger_matches_questrade(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            import_export(store, ROOT / "questrade_export.json", log=lambda *a, **k: None)
            issues = analytics.build(store)["issues"]
        problems = [i for i in issues if "Questrade reports" in i or "Couldn't match" in i]
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
