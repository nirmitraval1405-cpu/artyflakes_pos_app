"""GSTR-3B worksheet (Excel), laid out like the portal form: tables 3.1, 3.2, 4 and 5.

collect_purchases()  reads completed purchases (the only part that touches Django besides gstr1.collect)
build_3b()           pure Python: turns sales, returns and purchases into the figures
build_workbook()     writes an Excel file whose totals are formulas over two detail sheets
"""
from collections import defaultdict
from decimal import Decimal
from io import BytesIO

from .gstr1 import GSTIN_RE, STATES, ZERO, money, split_tax


def collect_purchases(date_from, date_to):
    from purchase.models import Purchase

    qs = (Purchase.objects.filter(status="completed", purchase_date__gte=date_from, purchase_date__lte=date_to)
          .select_related("supplier").prefetch_related("items").order_by("purchase_date", "id"))
    rows = []
    for p in qs:
        g = (p.supplier.gstin or "").strip().upper()
        rows.append({
            "no": p.purchase_no, "supplier_invoice": p.supplier_invoice_no, "date": p.purchase_date,
            "supplier": p.supplier.name, "gstin": g if GSTIN_RE.match(g) else "",
            "taxable": p.taxable_amount, "igst": p.igst, "cgst": p.cgst, "sgst": p.sgst, "total": p.grand_total,
            "zero_value": sum((i.total_amount for i in p.items.all() if i.gst_rate == 0), ZERO),
        })
    return rows


def build_3b(sales, returns, purchases, shop_gstin):
    shop_code = shop_gstin[:2]
    if shop_code not in STATES:
        raise ValueError("Your GSTIN in Store Settings does not start with a valid state code.")
    warnings = []

    def is_inter(gstin):
        code = gstin[:2] if gstin else shop_code
        return code in STATES and code != shop_code

    # ---- outward: (rate, inter-state) -> [taxable, tax], net of returns
    outward = defaultdict(lambda: [ZERO, ZERO])
    nil_out = ZERO
    for docs, sign in ((sales, +1), (returns, -1)):
        for d in docs:
            if sign == +1 and d["status"] != "completed":
                continue
            inter = is_inter(d["gstin"])
            for ln in d["lines"]:
                if ln["rate"] > 0:
                    cell = outward[(Decimal(ln["rate"]), inter)]
                    cell[0] += sign * ln["taxable"]
                    cell[1] += sign * ln["tax"]
                else:
                    nil_out += sign * ln["total"]
    outward_rows = []
    for (rate, inter), (taxable, tax) in sorted(outward.items()):
        if taxable == 0 and tax == 0:
            continue
        cgst, sgst = (ZERO, ZERO) if inter else split_tax(tax)
        outward_rows.append({"rate": rate, "inter": inter, "taxable": taxable,
                             "igst": tax if inter else ZERO, "cgst": cgst, "sgst": sgst})

    # ---- purchases: ITC only from suppliers with a GSTIN
    purchase_rows = []
    inward_nil = {"inter": ZERO, "intra": ZERO}
    no_invoice = 0
    for p in purchases:
        tax = p["igst"] + p["cgst"] + p["sgst"]
        inter = is_inter(p["gstin"])
        claim = bool(p["gstin"]) and tax > 0
        if tax > 0 and not p["gstin"]:
            warnings.append(f"Purchase {p['no']}: the supplier has no valid GSTIN, so its GST of {money(tax)} is not claimed as ITC.")
        if p["gstin"] and tax > 0:
            if (inter and (p["cgst"] or p["sgst"])) or (not inter and p["igst"]):
                warnings.append(f"Purchase {p['no']}: the tax type (IGST or CGST+SGST) does not match the supplier's state. "
                                "Check it before claiming ITC.")
            if not p["supplier_invoice"]:
                no_invoice += 1
        inward_nil["inter" if inter else "intra"] += p["zero_value"]
        purchase_rows.append({**p, "claim": claim})
    if no_invoice:
        warnings.append(f"{no_invoice} purchase(s) have no supplier invoice number, which you need to match them with GSTR-2B.")

    sums = lambda rows, key: sum((r[key] for r in rows), ZERO)
    itc = [r for r in purchase_rows if r["claim"]]
    totals = {
        "out_taxable": sums(outward_rows, "taxable"), "out_igst": sums(outward_rows, "igst"),
        "out_cgst": sums(outward_rows, "cgst"), "out_sgst": sums(outward_rows, "sgst"),
        "itc_igst": sums(itc, "igst"), "itc_cgst": sums(itc, "cgst"), "itc_sgst": sums(itc, "sgst"),
        "nil_out": nil_out, "inward_nil_inter": inward_nil["inter"], "inward_nil_intra": inward_nil["intra"],
    }
    return {"outward": outward_rows, "purchases": purchase_rows, "nil_out": nil_out, "inward_nil": inward_nil,
            "totals": totals, "warnings": warnings}


# --------------------------------------------------------------------------- workbook
def build_workbook(data, period_label, gstin, shop_name):
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    FONT = "Arial"
    plain, bold = Font(name=FONT, size=10), Font(name=FONT, size=10, bold=True)
    blue = Font(name=FONT, size=10, color="0000FF")
    head_fill = PatternFill("solid", fgColor="D9E1F2")
    manual_fill = PatternFill("solid", fgColor="FFFF00")
    thin = Side(style="thin", color="999999")
    box = Border(top=thin, bottom=thin, left=thin, right=thin)
    MONEY_FMT = "#,##0.00"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "GSTR-3B"
    so = wb.create_sheet("Outward supplies")
    pu = wb.create_sheet("ITC purchases")

    def put(sheet, ref, value, font=plain, fmt=None, fill=None, wrap=False, border=True):
        c = sheet[ref]
        c.value = value
        c.font = font
        if fmt:
            c.number_format = fmt
        if fill:
            c.fill = fill
        if wrap:
            c.alignment = Alignment(wrap_text=True, vertical="top")
        if border:
            c.border = box
        return c

    # ---- sheet: Outward supplies (net of returns), one row per rate and supply type
    so["A1"].value = f"Outward taxable supplies, net of returns: {period_label}"
    so["A1"].font = Font(name=FONT, size=12, bold=True)
    for col, text in zip("ABCDEF", ["Rate %", "Supply", "Taxable value", "IGST", "CGST", "SGST"]):
        put(so, f"{col}4", text, bold, fill=head_fill)
    for i, r in enumerate(data["outward"]):
        row = 5 + i
        put(so, f"A{row}", float(r["rate"]), blue)
        put(so, f"B{row}", "Inter-State" if r["inter"] else "Intra-State", blue)
        for col, key in zip("CDEF", ["taxable", "igst", "cgst", "sgst"]):
            put(so, f"{col}{row}", money(r[key]), blue, MONEY_FMT)
    last_o = max(5, 4 + len(data["outward"]))
    for col, w in zip("ABCDEF", [10, 14, 16, 14, 14, 14]):
        so.column_dimensions[col].width = w

    # ---- sheet: ITC purchases
    pu["A1"].value = f"Purchases in {period_label}. ITC is claimed only where the supplier has a GSTIN."
    pu["A1"].font = Font(name=FONT, size=12, bold=True)
    heads = ["Purchase no.", "Supplier invoice no.", "Date", "Supplier", "Supplier GSTIN",
             "Taxable value", "IGST", "CGST", "SGST", "Total", "ITC claimed"]
    for col, text in zip("ABCDEFGHIJK", heads):
        put(pu, f"{col}4", text, bold, fill=head_fill)
    for i, p in enumerate(data["purchases"]):
        row = 5 + i
        values = [p["no"], p["supplier_invoice"], p["date"], p["supplier"], p["gstin"] or "(none)",
                  money(p["taxable"]), money(p["igst"]), money(p["cgst"]), money(p["sgst"]), money(p["total"]),
                  "Yes" if p["claim"] else "No"]
        for col, v in zip("ABCDEFGHIJK", values):
            c = put(pu, f"{col}{row}", v, blue, MONEY_FMT if col in "FGHIJ" else None)
            if col == "C":
                c.number_format = "dd-mmm-yy"
    last_p = max(5, 4 + len(data["purchases"]))
    for col, w in zip("ABCDEFGHIJK", [16, 20, 12, 28, 18, 14, 12, 12, 12, 14, 12]):
        pu.column_dimensions[col].width = w

    # ---- main sheet
    ws["A1"].value = f"GSTR-3B worksheet: {period_label}"
    ws["A1"].font = Font(name=FONT, size=14, bold=True)
    ws["A2"].value = f"{shop_name}   GSTIN: {gstin}"
    ws["A2"].font = bold
    ws["A3"].value = ("Prepared from Artyflakes POS. Check it against your GSTR-1 and GSTR-2B before filing. "
                      "Blue = figures from the POS, black = formulas, yellow = not tracked, enter by hand if you have any.")
    ws["A3"].font = Font(name=FONT, size=9, italic=True)
    for col, w in zip("ABCDEF", [64, 18, 16, 16, 16, 12]):
        ws.column_dimensions[col].width = w

    r = 5

    def section(title, labels):
        nonlocal r
        put(ws, f"A{r}", title, bold, fill=head_fill, wrap=True)
        for col, text in zip("BCDEF", labels):
            put(ws, f"{col}{r}", text, bold, fill=head_fill, wrap=True)
        r += 1

    def line(label, values, manual=False, font=plain, fmt=MONEY_FMT):
        """values: dict column -> number or formula. Returns the row number."""
        nonlocal r
        put(ws, f"A{r}", label, font, wrap=True)
        for col in "BCDEF":
            if col in values:
                v = values[col]
                is_formula = isinstance(v, str) and v.startswith("=")
                put(ws, f"{col}{r}", v, font if is_formula or manual else blue, fmt, manual_fill if manual else None)
            else:
                put(ws, f"{col}{r}", None)
        r += 1
        return r - 1

    O = f"'Outward supplies'!"
    P = f"'ITC purchases'!"
    zero5 = {c: 0 for c in "BCDEF"}

    section("3.1  Outward supplies and inward supplies liable to reverse charge",
            ["Taxable value", "Integrated tax", "Central tax", "State/UT tax", "Cess"])
    a = line("(a) Outward taxable supplies (other than zero rated, nil rated and exempted)", {
        "B": f"=SUM({O}C5:C{last_o})", "C": f"=SUM({O}D5:D{last_o})",
        "D": f"=SUM({O}E5:E{last_o})", "E": f"=SUM({O}F5:F{last_o})", "F": 0})
    line("(b) Outward taxable supplies (zero rated)", zero5, manual=True)
    line("(c) Other outward supplies (nil rated, exempted)", {"B": money(data["nil_out"])})
    line("(d) Inward supplies (liable to reverse charge)", zero5, manual=True)
    line("(e) Non-GST outward supplies", {"B": 0}, manual=True)
    r += 1

    section("3.2  Inter-State supplies made to unregistered persons, composition dealers and UIN holders",
            ["Taxable value", "Integrated tax", "", "", ""])
    line("Supplies made to unregistered persons (Inter-State). Your walk-in sales are treated as in-state.",
         {"B": 0, "C": 0}, manual=True)
    line("Supplies made to composition taxable persons", {"B": 0, "C": 0}, manual=True)
    line("Supplies made to UIN holders", {"B": 0, "C": 0}, manual=True)
    r += 1

    section("4  Eligible ITC", ["", "Integrated tax", "Central tax", "State/UT tax", "Cess"])
    a1 = line("(A)(1) Import of goods", {c: 0 for c in "CDEF"}, manual=True)
    line("(A)(2) Import of services", {c: 0 for c in "CDEF"}, manual=True)
    line("(A)(3) Inward supplies liable to reverse charge", {c: 0 for c in "CDEF"}, manual=True)
    line("(A)(4) Inward supplies from ISD", {c: 0 for c in "CDEF"}, manual=True)
    a5 = line("(A)(5) All other ITC", {
        "C": f'=SUMIFS({P}G5:G{last_p},{P}K5:K{last_p},"Yes")',
        "D": f'=SUMIFS({P}H5:H{last_p},{P}K5:K{last_p},"Yes")',
        "E": f'=SUMIFS({P}I5:I{last_p},{P}K5:K{last_p},"Yes")', "F": 0})
    atot = line("(A) ITC available (sum of A1 to A5)", {c: f"=SUM({c}{a1}:{c}{a5})" for c in "CDEF"}, font=bold)
    b1 = line("(B)(1) ITC reversed: as per rules 38, 42 and 43 of CGST Rules and section 17(5)",
              {c: 0 for c in "CDEF"}, manual=True)
    b2 = line("(B)(2) ITC reversed: others", {c: 0 for c in "CDEF"}, manual=True)
    ctot = line("(C) Net ITC available (A - B)", {c: f"={c}{atot}-SUM({c}{b1}:{c}{b2})" for c in "CDEF"}, font=bold)
    line("(D)(1) Ineligible ITC: as per section 17(5)", {c: 0 for c in "CDEF"}, manual=True)
    line("(D)(2) Ineligible ITC: others", {c: 0 for c in "CDEF"}, manual=True)
    r += 1

    section("5  Exempt, nil-rated and non-GST inward supplies", ["Inter-State supplies", "Intra-State supplies", "", "", ""])
    line("From a supplier under composition scheme, exempt and nil rated supply",
         {"B": money(data["inward_nil"]["inter"]), "C": money(data["inward_nil"]["intra"])})
    line("Non-GST supply", {"B": 0, "C": 0}, manual=True)
    r += 1

    section("Indicative tax to pay (before the set-off order in table 6.1)",
            ["", "Integrated tax", "Central tax", "State/UT tax", "Cess"])
    out_row = line("Output tax (3.1)", {"C": f"=C{a}+C{a+1}+C{a+3}", "D": f"=D{a}+D{a+1}+D{a+3}",
                                         "E": f"=E{a}+E{a+1}+E{a+3}", "F": f"=F{a}+F{a+1}+F{a+3}"})
    itc_row = line("Net ITC (4C)", {c: f"={c}{ctot}" for c in "CDEF"})
    line("Output tax minus ITC. A negative figure is credit left over.",
         {c: f"={c}{out_row}-{c}{itc_row}" for c in "CDEF"}, font=bold)

    ws.freeze_panes = "A5"
    for sheet in (ws, so, pu):
        sheet.sheet_view.showGridLines = False
    wb.calculation.fullCalcOnLoad = True
    out = BytesIO()
    wb.save(out)
    return out.getvalue()