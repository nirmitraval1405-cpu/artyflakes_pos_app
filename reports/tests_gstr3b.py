from datetime import date
from decimal import Decimal as D
from io import BytesIO

from django.test import SimpleTestCase

from . import gstr3b
from .tests_gstr1 import OTHER_STATE, SAME_STATE, line, ret, sale


def purchase(no, gstin, taxable, igst="0", cgst="0", sgst="0", zero="0", invoice="S-1"):
    return {"no": no, "supplier_invoice": invoice, "date": date(2026, 7, 5), "supplier": "Supplier", "gstin": gstin,
            "taxable": D(taxable), "igst": D(igst), "cgst": D(cgst), "sgst": D(sgst),
            "total": D(taxable) + D(igst) + D(cgst) + D(sgst), "zero_value": D(zero)}


class Gstr3bTests(SimpleTestCase):
    def test_outward_tax_is_split_by_state_and_net_of_returns(self):
        sales = [
            sale("AF-2026-0001", "12744", OTHER_STATE, [line("Pen", "9608", 150, 18, "12744")]),
            sale("AF-2026-0002", "1180", "", [line("Box", "3213", 2, 18, "1180")]),
        ]
        returns = [ret("AF-R-2026-0001", "590", "", [line("Box", "3213", 1, 18, "590")])]
        t = gstr3b.build_3b(sales, returns, [], SAME_STATE)["totals"]
        self.assertEqual((t["out_taxable"], t["out_igst"]), (D("11300.00"), D("1944.00")))
        self.assertEqual((t["out_cgst"], t["out_sgst"]), (D("45.00"), D("45.00")))

    def test_nil_rated_sales_and_voided_bills(self):
        sales = [
            sale("AF-2026-0001", "800", "", [line("Notebook", "4820", 5, 0, "800")]),
            sale("AF-2026-0002", "999", "", [], status="voided"),
        ]
        d = gstr3b.build_3b(sales, [], [], SAME_STATE)
        self.assertEqual(d["nil_out"], D("800"))
        self.assertEqual(d["totals"]["out_taxable"], D("0"))

    def test_itc_only_from_suppliers_with_a_gstin(self):
        d = gstr3b.build_3b([], [], [
            purchase("PU-1", SAME_STATE, "1000", cgst="90", sgst="90"),
            purchase("PU-2", OTHER_STATE, "5000", igst="600", zero="800"),
            purchase("PU-3", "", "200", cgst="12", sgst="12"),
        ], SAME_STATE)
        t = d["totals"]
        self.assertEqual((t["itc_igst"], t["itc_cgst"], t["itc_sgst"]), (D("600"), D("90"), D("90")))
        self.assertEqual(d["inward_nil"], {"inter": D("800"), "intra": D("0")})
        self.assertTrue(any("PU-3" in w and "not claimed" in w for w in d["warnings"]))

    def test_warns_when_tax_type_does_not_match_the_suppliers_state(self):
        d = gstr3b.build_3b([], [], [purchase("PU-1", OTHER_STATE, "1000", cgst="90", sgst="90")], SAME_STATE)
        self.assertTrue(any("does not match" in w for w in d["warnings"]))

    def test_workbook_totals_are_formulas_over_the_detail_sheets(self):
        import openpyxl

        sales = [sale("AF-2026-0001", "1180", "", [line("Box", "3213", 2, 18, "1180")])]
        d = gstr3b.build_3b(sales, [], [purchase("PU-1", SAME_STATE, "1000", cgst="90", sgst="90")], SAME_STATE)
        wb = openpyxl.load_workbook(BytesIO(gstr3b.build_workbook(d, "July 2026", SAME_STATE, "Shop")))
        self.assertEqual(wb.sheetnames, ["GSTR-3B", "Outward supplies", "ITC purchases"])
        self.assertTrue(wb["GSTR-3B"]["B6"].value.startswith("=SUM("))
        self.assertEqual(wb["Outward supplies"]["C5"].value, 1000.0)
        self.assertEqual(wb["ITC purchases"]["K5"].value, "Yes")