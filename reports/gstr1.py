"""GSTR-1 export in the GSTN offline-tool workbook format.

Two layers:
  * collect()          reads sales and returns from the database into plain dicts (the only part that touches Django)
  * build_sections()   turns those dicts into the rows of each sheet (pure Python, easy to test)
  * build_workbook()   writes the rows into a slim copy of the official template (reports/assets/gstr1_template.xlsx)
"""
import re
from collections import defaultdict
from copy import copy
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from pathlib import Path

from pos.gst import split_tax

TWO = Decimal("0.01")
ZERO = Decimal("0")
TEMPLATE = Path(__file__).resolve().parent / "assets" / "gstr1_template.xlsx"
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
DOC_NO_RE = re.compile(r"^[A-Za-z0-9/-]{1,16}$")

# Exactly the strings in the template's "master" sheet (they feed the portal's drop-downs).
STATES = {s[:2]: s for s in [
    "01-Jammu & Kashmir", "02-Himachal Pradesh", "03-Punjab", "04-Chandigarh", "05-Uttarakhand", "06-Haryana",
    "07-Delhi", "08-Rajasthan", "09-Uttar Pradesh", "10-Bihar", "11-Sikkim", "12-Arunachal Pradesh",
    "13-Nagaland", "14-Manipur", "15-Mizoram", "16-Tripura", "17-Meghalaya", "18-Assam", "19-West Bengal",
    "20-Jharkhand", "21-Odisha", "22-Chhattisgarh", "23-Madhya Pradesh", "24-Gujarat", "25-Daman & Diu",
    "26-Dadra & Nagar Haveli & Daman & Diu", "27-Maharashtra", "29-Karnataka", "30-Goa", "31-Lakshdweep",
    "32-Kerala", "33-Tamil Nadu", "34-Puducherry", "35-Andaman & Nicobar Islands", "36-Telangana",
    "37-Andhra Pradesh", "38-Ladakh", "97-Other Territory",
]}

# Product.unit value -> UQC text from the template's master sheet
UQC = {
    "pcs": "NOS-NUMBERS", "set": "SET-SETS", "box": "BOX-BOX", "pack": "PAC-PACKS", "kg": "KGS-KILOGRAMS",
    "g": "GMS-GRAMMES", "ltr": "LTR-LITRES", "ml": "MLT-MILILITRE", "mtr": "MTR-METERS",
}

EXEMP_ROWS = [  # (description, registered?, inter-state?)
    ("Inter-State supplies to registered persons", True, True),
    ("Intra-State supplies to registered persons", True, False),
    ("Inter-State supplies to unregistered persons", False, True),
    ("Intra-State supplies to unregistered persons", False, False),
]


def money(value):
    return float(Decimal(value).quantize(TWO, rounding=ROUND_HALF_UP))


def num(value):
    """5 instead of 5.0, 0.25 stays 0.25."""
    value = Decimal(value).normalize()
    return int(value) if value == value.to_integral_value() else float(value)


def hsn_cell(code):
    return int(code) if code.isdigit() and not code.startswith("0") else code


def split_no(number):
    m = re.match(r"^(.*?)(\d+)$", number)
    return (m.group(1), int(m.group(2))) if m else (number, 0)


# --------------------------------------------------------------------------- aggregation (pure Python)
def build_sections(sales, returns, shop_gstin):
    """sales/returns: lists of dicts, see collect(). Returns {"sheets", "summary", "warnings"}."""
    warnings = []
    shop_code = shop_gstin[:2]
    if shop_code not in STATES:
        raise ValueError("Your GSTIN in Store Settings does not start with a valid state code.")

    b2b, cdnr = [], []
    b2cs = defaultdict(Decimal)            # (pos, rate) -> net taxable value
    nil = defaultdict(Decimal)             # (registered, inter_state) -> net value of 0% supplies
    hsn = {True: {}, False: {}}            # registered -> {(hsn, uqc, rate): totals}
    missing_hsn = defaultdict(int)
    tax = {"b2b": ZERO, "b2cs": ZERO, "cdnr": ZERO}
    taxable = {"b2b": ZERO, "b2cs": ZERO, "cdnr": ZERO}

    def party(gstin, ref):
        if gstin:
            code = gstin[:2]
            if code in STATES:
                return True, code, STATES[code]
            warnings.append(f"{ref}: the GSTIN state code {code} is not in the GST state list, so your own state was used.")
            return True, shop_code, STATES[shop_code]
        return False, shop_code, STATES[shop_code]

    def add_hsn(registered, inter, ln, sign):
        code = (ln["hsn"] or "").strip()
        if not code:
            missing_hsn[ln["name"]] += 1
            return
        key = (code, UQC.get((ln["unit"] or "").lower(), "OTH-OTHERS"), Decimal(ln["rate"]))
        a = hsn[registered].setdefault(key, {
            "qty": ZERO, "value": ZERO, "taxable": ZERO, "igst": ZERO, "cs": ZERO, "names": defaultdict(Decimal)})
        a["qty"] += sign * ln["qty"]
        a["value"] += sign * ln["total"]
        a["taxable"] += sign * ln["taxable"]
        a["igst" if inter else "cs"] += sign * ln["tax"]
        a["names"][ln["name"]] += ln["total"]

    # ---- sales
    for s in sales:
        if s["status"] != "completed":
            continue
        registered, code, pos = party(s["gstin"], s["invoice_no"])
        inter = code != shop_code
        per_rate = defaultdict(lambda: [ZERO, ZERO])
        for ln in s["lines"]:
            if ln["rate"] > 0:
                per_rate[Decimal(ln["rate"])][0] += ln["taxable"]
                per_rate[Decimal(ln["rate"])][1] += ln["tax"]
            else:
                nil[(registered, inter)] += ln["total"]
            add_hsn(registered, inter, ln, +1)
        for rate, (base, t) in sorted(per_rate.items()):
            if registered:
                b2b.append([s["gstin"], s["name"], s["invoice_no"], s["date"], money(s["total"]), pos, "N", None,
                            "Regular B2B", None, num(rate), money(base), None])
                taxable["b2b"] += base
                tax["b2b"] += t
            else:
                b2cs[(pos, rate)] += base
                taxable["b2cs"] += base
                tax["b2cs"] += t

    # ---- sales returns (credit notes)
    for r in returns:
        registered, code, pos = party(r["gstin"], r["return_no"])
        inter = code != shop_code
        per_rate = defaultdict(lambda: [ZERO, ZERO])
        for ln in r["lines"]:
            if ln["rate"] > 0:
                per_rate[Decimal(ln["rate"])][0] += ln["taxable"]
                per_rate[Decimal(ln["rate"])][1] += ln["tax"]
            else:
                nil[(registered, inter)] -= ln["total"]
            add_hsn(registered, inter, ln, -1)
        for rate, (base, t) in sorted(per_rate.items()):
            if registered:
                cdnr.append([r["gstin"], r["name"], r["return_no"], r["date"], "C", pos, "N", "Regular B2B",
                             money(r["refund_total"]), None, num(rate), money(base), None])
                taxable["cdnr"] += base
                tax["cdnr"] += t
            else:
                b2cs[(pos, rate)] -= base      # credit notes to unregistered buyers are netted in Table 7
                taxable["b2cs"] -= base
                tax["b2cs"] -= t

    b2cs_rows = []
    for (pos, rate), base in sorted(b2cs.items()):
        if base == 0:
            continue
        if base < 0:
            warnings.append(f"B2CS {pos} at {num(rate)}%: returns are larger than sales in this period (net {money(base)}). "
                            "Check this row before filing.")
        b2cs_rows.append(["OE", pos, None, num(rate), money(base), None, None])

    exemp_rows = []
    for text, registered, inter in EXEMP_ROWS:
        value = nil.get((registered, inter), ZERO)
        exemp_rows.append([text, money(value) if value else None, None, None])

    hsn_rows = {}
    for registered, bucket in hsn.items():
        rows = []
        for (code, uqc, rate), a in sorted(bucket.items(), key=lambda kv: (kv[0][0], kv[0][2], kv[0][1])):
            if a["qty"] == 0 and a["value"] == 0:
                continue
            cgst, sgst = split_tax(a["cs"])
            name = max(a["names"].items(), key=lambda kv: kv[1])[0][:30]
            rows.append([hsn_cell(code), name, uqc, num(a["qty"]), money(a["value"]), num(rate), money(a["taxable"]),
                         money(a["igst"]) if a["igst"] else None, money(cgst) if cgst else None,
                         money(sgst) if sgst else None, None])
        hsn_rows[registered] = rows

    # ---- Table 13: documents issued
    docs_rows = []
    for nature, items in (
        ("Invoices for outward supply", [(s["invoice_no"], s["status"] != "completed") for s in sales]),
        ("Credit Note", [(r["return_no"], False) for r in returns]),
    ):
        groups = defaultdict(list)
        for number, cancelled in items:
            if not DOC_NO_RE.match(number):
                warnings.append(f"Document number {number} is longer than 16 characters or has symbols GST does not allow.")
            prefix, n = split_no(number)
            groups[prefix].append((n, number, cancelled))
        for prefix, docs in sorted(groups.items()):
            docs.sort()
            first, last = docs[0], docs[-1]
            if last[0] - first[0] + 1 != len(docs):
                warnings.append(f"{nature}: numbers {first[1]} to {last[1]} have {last[0] - first[0] + 1 - len(docs)} "
                                "missing number(s). GST expects a gap-free series.")
            docs_rows.append([nature, first[1], last[1], len(docs), sum(1 for d in docs if d[2])])

    if missing_hsn:
        sample = ", ".join(list(missing_hsn)[:5])
        warnings.append(f"{sum(missing_hsn.values())} line(s) have no HSN code and are left out of the HSN sheets "
                        f"(for example: {sample}). Add the HSN to those products.")

    sheets = {
        "b2b,sez,de": b2b, "b2cs": b2cs_rows, "cdnr": cdnr, "exemp": exemp_rows,
        "hsn(b2b)": hsn_rows[True], "hsn(b2c)": hsn_rows[False], "docs": docs_rows,
    }
    summary = [
        {"section": "B2B invoices (4A)", "rows": len(b2b), "docs": len({r[2] for r in b2b}),
         "taxable": taxable["b2b"], "tax": tax["b2b"]},
        {"section": "B2C small, net of returns (7)", "rows": len(b2cs_rows), "docs": None,
         "taxable": taxable["b2cs"], "tax": tax["b2cs"]},
        {"section": "Credit notes to registered buyers (9B)", "rows": len(cdnr), "docs": len({r[2] for r in cdnr}),
         "taxable": taxable["cdnr"], "tax": tax["cdnr"]},
        {"section": "Nil rated / exempt (8)", "rows": sum(1 for r in exemp_rows if r[1]), "docs": None,
         "taxable": sum(nil.values(), ZERO), "tax": ZERO},
        {"section": "HSN summary, registered buyers (12)", "rows": len(hsn_rows[True]), "docs": None,
         "taxable": sum((Decimal(str(r[6])) for r in hsn_rows[True]), ZERO), "tax": None},
        {"section": "HSN summary, unregistered buyers (12)", "rows": len(hsn_rows[False]), "docs": None,
         "taxable": sum((Decimal(str(r[6])) for r in hsn_rows[False]), ZERO), "tax": None},
        {"section": "Documents issued (13)", "rows": len(docs_rows), "docs": sum(r[3] for r in docs_rows),
         "taxable": None, "tax": None},
    ]
    return {"sheets": sheets, "summary": summary, "warnings": warnings}


# --------------------------------------------------------------------------- workbook
def build_workbook(sheets):
    """Writes the rows into a copy of the slim template. Row 5 of each sheet is the style row."""
    import openpyxl

    wb = openpyxl.load_workbook(TEMPLATE)
    for name, rows in sheets.items():
        ws = wb[name]
        styles = [copy(c._style) for c in ws[5]]
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                cell = ws.cell(row=5 + i, column=j + 1)
                if j < len(styles):
                    cell._style = copy(styles[j])
                if value is not None and value != "":
                    cell.value = value
    wb.calculation.fullCalcOnLoad = True      # Excel fills in the totals in rows 2-3 on opening
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


# --------------------------------------------------------------------------- database
def _gstin(contact):
    if contact is None or contact.is_system:
        return ""
    g = (contact.gstin or "").strip().upper()
    return g if GSTIN_RE.match(g) else ""


def collect(date_from, date_to):
    """Reads the period's sales and returns. Dates are inclusive."""
    from django.utils import timezone
    from pos.models import Sale, SaleReturn
    from storeconfig.models import StoreSettings

    shop_gstin = (StoreSettings.load().gstin or "").strip().upper()

    sales = []
    qs = (Sale.objects.filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
          .select_related("customer").prefetch_related("items").order_by("invoice_no"))
    for s in qs:
        sales.append({
            "invoice_no": s.invoice_no, "date": timezone.localtime(s.created_at).date(), "status": s.status,
            "total": s.total, "name": s.customer.name, "gstin": _gstin(s.customer),
            "lines": [
                {"name": i.product_name, "hsn": i.hsn_code, "unit": i.unit, "qty": i.quantity, "rate": i.gst_rate,
                 "taxable": i.line_total - i.tax_amount, "tax": i.tax_amount, "total": i.line_total}
                for i in s.items.all()
            ] if s.status == "completed" else [],
        })

    returns = []
    qs = (SaleReturn.objects.filter(created_at__date__gte=date_from, created_at__date__lte=date_to,
                                    sale__status="completed")
          .select_related("sale__customer").prefetch_related("items__sale_item").order_by("return_no"))
    for r in qs:
        returns.append({
            "return_no": r.return_no, "date": timezone.localtime(r.created_at).date(), "refund_total": r.refund_total,
            "name": r.sale.customer.name, "gstin": _gstin(r.sale.customer),
            "lines": [
                {"name": ri.product_name, "hsn": ri.sale_item.hsn_code, "unit": ri.sale_item.unit, "qty": ri.quantity,
                 "rate": ri.sale_item.gst_rate, "taxable": ri.refund_amount - ri.tax_amount, "tax": ri.tax_amount,
                 "total": ri.refund_amount}
                for ri in r.items.all()
            ],
        })
    return shop_gstin, sales, returns