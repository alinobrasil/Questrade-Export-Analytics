import unittest
from unittest import mock

import sheet_sync
from sheet_sync import account_label, parse_date, plan, sheet_symbol, shift_formula

# Made-up labels: holdings of VOO belong to "Kid" (the real ones live in config.json).
LABELS = {"VOO": {"TFSA": "tfsa-kid", "RRSP": "RRSP Kid"}}


def setUpModule():
    patcher = mock.patch.object(sheet_sync, "ACCOUNT_LABELS", LABELS)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)

HEADER = ["Type - Buy or Sell", "Account Type (RRSP?)", "Broker", "Date", "stock ", "qty change", "Currency",
          "transaction price"]
SEP11 = 46276  # 9/11/2026 as a sheet serial number


def sheet_row(n, account, symbol, qty, price, serial=SEP11):
    return ["Buy", account, "questrade", serial, symbol, qty, "USD", price, f"=H{n}*F{n}", f"=abs(I{n})"]


def trade(symbol, qty, price, day="2026-09-11", currency="USD", type_="Trades", net=0.0):
    return {"tradeDate": f"{day}T00:00:00.000000-04:00", "type": type_, "action": "Buy" if qty > 0 else "Sell",
            "symbol": symbol, "quantity": qty, "price": price, "netAmount": net, "currency": currency}


# The sheet as it stands: 9/11 rows for FHSA VTI, FHSA VOO split in two, RRSP VOO labelled plain "RRSP".
SHEET = [HEADER,
         sheet_row(2, "FHSA", "VTI", 7.9553, 377.1049),
         sheet_row(3, "FHSA", "VOO", 3.4094, 703.93),
         sheet_row(4, "FHSA", "VOO", 0.1421, 703.59),
         sheet_row(5, "RRSP", "VOO", 1.9026, 703.2319)]

# What Questrade reports for 9/11 onwards.
TRADES = [("RRSP", trade("VOO", 1.9026, 703.2319)),
          ("FHSA", trade("VTI", 7.9553, 377.1049)),
          ("FHSA", trade("VFV.TO", 1.2172, 188.84, currency="CAD")),
          ("FHSA", trade("VOO", 3.5515, 703.921772))]


class MappingTest(unittest.TestCase):
    def test_symbols(self):
        self.assertEqual(sheet_symbol("VFV.TO"), "TSE:VFV")
        self.assertEqual(sheet_symbol("VOO"), "VOO")

    def test_renamed_labels(self):
        self.assertEqual(account_label("RRSP", "VOO"), "RRSP Kid")
        self.assertEqual(account_label("TFSA", "VOO"), "tfsa-kid")
        self.assertEqual(account_label("FHSA", "VOO"), "FHSA")
        self.assertEqual(account_label("TFSA", "VTI"), "TFSA")

    def test_dates(self):
        self.assertEqual(parse_date(SEP11), "2026-09-11")
        self.assertEqual(parse_date("46276"), "2026-09-11")
        self.assertEqual(parse_date("9/11/2026"), "2026-09-11")
        self.assertIsNone(parse_date("4/6"))


class FormulaTest(unittest.TestCase):
    def test_relative_rows_move(self):
        self.assertEqual(shift_formula("=H172*F172", 1), "=H173*F173")
        self.assertEqual(shift_formula('=year(D172)&"_"&if(month(D172)<10,"0"&month(D172),month(D172))', 2),
                         '=year(D174)&"_"&if(month(D174)<10,"0"&month(D174),month(D174))')

    def test_quotes_absolute_refs_and_other_sheets_stay(self):
        f = ('=if(G172="USD", K172, if(G172="CAD", K172/cash!$I$23, if(G172="BRL", K172/cash!$I$26, '
             'if(G172="KRW", K172/cash!$I$25)))) * F172')
        self.assertEqual(shift_formula(f, 1), f.replace("172", "173"))
        self.assertEqual(shift_formula('=index(GOOGLEFINANCE("CURRENCY:USDCAD","price", D172),2,2)', 1),
                         '=index(GOOGLEFINANCE("CURRENCY:USDCAD","price", D173),2,2)')
        self.assertEqual(shift_formula("=LOG10(A$5)+$B9", 1), "=LOG10(A$5)+$B10")


class PlanTest(unittest.TestCase):
    def test_only_the_missing_same_day_trade_is_added(self):
        last, last_date, new, warnings = plan(SHEET, TRADES)
        self.assertEqual((last, last_date), (5, "2026-09-11"))
        self.assertEqual(new, [["Buy", "FHSA", "questrade", "2026-09-11", "TSE:VFV", 1.2172, "CAD", 188.84]])
        self.assertEqual(warnings, [])

    def test_later_trades_are_added_with_renamed_labels(self):
        later = TRADES + [("TFSA", trade("VOO", 2, 710, day="2026-10-02")),
                          ("RRSP", trade("VTI", -3, 380, day="2026-10-02"))]
        _, _, new, _ = plan(SHEET, later)
        self.assertIn(["Buy", "tfsa-kid", "questrade", "2026-10-02", "VOO", 2, "USD", 710], new)
        self.assertIn(["Sell", "RRSP", "questrade", "2026-10-02", "VTI", -3, "USD", 380], new)

    def test_partial_same_day_mismatch_is_flagged_not_added(self):
        _, _, new, warnings = plan(SHEET, [("FHSA", trade("VTI", 9.0, 377.1))])
        self.assertEqual(new, [])
        self.assertEqual(len(warnings), 1)

    def test_reinvestment_price_from_amount(self):
        _, _, new, _ = plan(SHEET, [("RRSP", trade("VOO", 0.1597, 0, day="2026-09-30",
                                                   type_="Dividend reinvestment", net=-112.81))])
        self.assertAlmostEqual(new[0][7], 706.387, places=3)


if __name__ == "__main__":
    unittest.main()
