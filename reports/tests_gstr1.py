from datetime import date
from decimal import Decimal as D
from io import BytesIO

from django.test import SimpleTestCase

from . import gstr1

OTHER_STATE = "29AAOFM6905G1ZW"   # Karnataka
SAME_STATE = "24ABCDE1234F1Z5"    # Gujarat, the shop's own state


def line(name, hsn, qty, rate, total):
    total, rate = D(total), D(rate)
    taxable = (total * 100 / (100 + rate)).quantize(D("0.01"))
    return {"name": name, "hsn": hsn, "unit": "pcs", "qty": D(qty), "rate": rate,
            "taxable": taxable, "tax": total - taxable, "total": total}


def sale(no, total, gstin, lines, status="completed", name="Walk-in customer"):
    return {"invoice_no": no, "date": date(2026, 7, 18), "status": status, "total": D(total),
            "name": name, "gstin": gstin, "lines": lines}


def ret(no, total, gstin, lines):
    return {"return_no": no, "date": date(2026, 7, 25), "refund_total": D(total), "name": "x",
            "gstin": gstin, "lines": lines}


class Gstr1SectionTests(SimpleTestCase):
    def build(self, sales, returns=()):
        return gstr1.build_sections(sales, list(returns), SAME_STATE)

    def test_other_state_b2b_uses_igst_and_its_own_place_of_supply(self):
        r = self.build([sale("AF-2026-0001", "12744", OTHER_STATE, [line("Pen", "960810", 150, 18, "12744")])])
        row = r["sheets"]["b2b,sez,de"][0]
        self.assertEqual(row[:7], [OTHER_STATE, "Walk-in customer", "AF-2026-0001", date(2026, 7, 18), 12744.0, "29-Karnataka", "N"])
        self.assertEqual((row[8], row[10], row[11]), ("Regular B2B", 18, 10800.0))
        hsn = r["sheets"]["hsn(b2b)"][0]
        self.assertEqual((hsn[0], hsn[2], hsn[7], hsn[8], hsn[9]), (960810, "NOS-NUMBERS", 1944.0, None, None))

    def test_same_state_splits_cgst_and_sgst(self):
        r = self.build([sale("AF-2026-0001", "1180", SAME_STATE, [line("Resin", "39073010", 1, 18, "1180")])])
        hsn = r["sheets"]["hsn(b2b)"][0]
        self.assertEqual((hsn[7], hsn[8], hsn[9]), (None, 90.0, 90.0))

    def test_unregistered_sales_are_grouped_by_rate_in_b2cs(self):
        r = self.build([
            sale("AF-2026-0001", "5640.28", "", [line("Book", "482020", 188, 12, "5640.28")]),
            sale("AF-2026-0002", "112", "", [line("Book", "482020", 1, 12, "112")]),
        ])
        self.assertEqual(r["sheets"]["b2cs"], [["OE", "24-Gujarat", None, 12, 5135.96, None, None]])
        self.assertEqual(r["sheets"]["b2b,sez,de"], [])

    def test_returns_reduce_b2cs_and_go_to_cdnr_for_registered_buyers(self):
        r = self.build(
            [sale("AF-2026-0001", "2360", "", [line("Box", "32131000", 4, 18, "2360")]),
             sale("AF-2026-0002", "1180", SAME_STATE, [line("Box", "32131000", 2, 18, "1180")])],
            [ret("AF-R-2026-0001", "590", "", [line("Box", "32131000", 1, 18, "590")]),
             ret("AF-R-2026-0002", "590", SAME_STATE, [line("Box", "32131000", 1, 18, "590")])],
        )
        self.assertEqual(r["sheets"]["b2cs"][0][4], 1500.0)        # 2000 - 500
        note = r["sheets"]["cdnr"][0]
        self.assertEqual((note[2], note[4], note[8], note[10], note[11]), ("AF-R-2026-0002", "C", 590.0, 18, 500.0))

    def test_hsn_value_is_net_of_returns_and_taxable_plus_tax_equals_value(self):
        r = self.build(
            [sale("AF-2026-0001", "1180", "", [line("Box", "32131000", 2, 18, "1180")])],
            [ret("AF-R-2026-0001", "590", "", [line("Box", "32131000", 1, 18, "590")])],
        )
        row = r["sheets"]["hsn(b2c)"][0]
        self.assertEqual((row[3], row[4], row[6]), (1, 590.0, 500.0))
        self.assertAlmostEqual(row[6] + row[8] + row[9], row[4], places=2)

    def test_zero_rated_lines_go_to_exemp_not_b2b(self):
        r = self.build([sale("AF-2026-0001", "800", SAME_STATE, [line("Notebook", "4820", 5, 0, "800")])])
        self.assertEqual(r["sheets"]["b2b,sez,de"], [])
        self.assertEqual(r["sheets"]["exemp"][1], ["Intra-State supplies to registered persons", 800.0, None, None])

    def test_voided_sales_are_counted_as_cancelled_and_not_taxed(self):
        r = self.build([
            sale("AF-2026-0001", "118", "", [line("Pen", "9608", 1, 18, "118")]),
            sale("AF-2026-0002", "999", "", [], status="voided"),
        ])
        self.assertEqual(r["sheets"]["docs"], [["Invoices for outward supply", "AF-2026-0001", "AF-2026-0002", 2, 1]])
        self.assertEqual(r["sheets"]["b2cs"][0][4], 100.0)

    def test_gaps_missing_hsn_and_bad_state_codes_produce_warnings(self):
        r = self.build([
            sale("AF-2026-0001", "118", "", [line("Pen", "", 1, 18, "118")]),
            sale("AF-2026-0003", "118", "28AAOFM6905G1ZW", [line("Pen", "9608", 1, 18, "118")]),
        ])
        text = " ".join(r["warnings"])
        self.assertIn("missing number", text)
        self.assertIn("no HSN code", text)
        self.assertIn("state code 28", text)

    def test_shop_gstin_must_have_a_valid_state_code(self):
        with self.assertRaises(ValueError):
            gstr1.build_sections([], [], "99AAAAA0000A1Z5")


class Gstr1WorkbookTests(SimpleTestCase):
    def test_workbook_keeps_the_template_layout_and_writes_rows_from_row_5(self):
        import openpyxl

        rows = {"b2cs": [["OE", "24-Gujarat", None, 12, 5035.96, None, None]]}
        wb = openpyxl.load_workbook(BytesIO(gstr1.build_workbook(rows)))
        self.assertEqual(len(wb.sheetnames), 32)
        self.assertEqual(wb["b2cs"]["A4"].value, "Type")
        self.assertEqual(wb["b2cs"]["B5"].value, "24-Gujarat")
        self.assertEqual(wb["b2cs"]["E5"].value, 5035.96)
        self.assertEqual(wb["b2cs"]["E5"].number_format, "0.00")