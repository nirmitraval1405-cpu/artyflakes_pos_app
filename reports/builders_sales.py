from collections import defaultdict
from decimal import Decimal

from django.db.models import Count, Max, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from dashboard.views import PROFIT_BACK, PROFIT_SOLD
from pos.gst import split_tax
from pos.models import Payment, Sale, SaleItem, SaleReturn, SaleReturnItem

from .helpers import Col, Report
from .registry import register

ZERO = Decimal("0")


def _sales(p):
    """Completed bills in the period (voided bills never count)."""
    qs = Sale.objects.filter(
        status=Sale.Status.COMPLETED, created_at__date__gte=p.date_from, created_at__date__lte=p.date_to
    )
    if p.party:
        qs = qs.filter(customer_id=p.party)
    if p.user:
        qs = qs.filter(cashier_id=p.user)
    return qs


def _returns(p):
    """Returns made in the period, counted on the day of the return."""
    qs = SaleReturn.objects.filter(created_at__date__gte=p.date_from, created_at__date__lte=p.date_to)
    if p.party:
        qs = qs.filter(sale__customer_id=p.party)
    if p.user:
        qs = qs.filter(processed_by_id=p.user)
    return qs


def _rate_groups(sales_qs):
    """{(sale_id, gst_rate): (taxable value, GST)}  the same grouping the invoice page uses."""
    rows = (
        SaleItem.objects.filter(sale__in=sales_qs).order_by().values("sale_id", "gst_rate")
        .annotate(t_line=Sum("line_total"), t_tax=Sum("tax_amount"))
    )
    return {(r["sale_id"], r["gst_rate"]): (r["t_line"] - r["t_tax"], r["t_tax"]) for r in rows}


def _local_date(dt):
    return timezone.localtime(dt).date()


# ---------- 1. Daily sales ----------
@register("Sales", "Daily sales", uses=("party", "user"))
def daily_sales(p):
    sold = (
        _sales(p).order_by().annotate(t_day=TruncDate("created_at")).values("t_day")
        .annotate(t_bills=Count("id"), t_sub=Sum("subtotal"), t_disc=Sum("discount_total"),
                  t_gst=Sum("tax_total"), t_net=Sum("total"))
    )
    back = (
        _returns(p).order_by().annotate(t_day=TruncDate("created_at")).values("t_day")
        .annotate(t_ref=Sum("refund_total"))
    )
    days = {}

    def row(d):
        return days.setdefault(d, {"day": d, "bills": 0, "sub": ZERO, "disc": ZERO, "gst": ZERO, "net": ZERO, "refund": ZERO})

    for r in sold:
        row(r["t_day"]).update(bills=r["t_bills"], sub=r["t_sub"], disc=r["t_disc"], gst=r["t_gst"], net=r["t_net"])
    for r in back:
        row(r["t_day"])["refund"] = r["t_ref"]
    records = [dict(v, after=v["net"] - v["refund"]) for _, v in sorted(days.items())]

    cols = [
        Col("Date", "day", "date"), Col("Bills", "bills", "int"), Col("Subtotal", "sub", "money"),
        Col("Discount", "disc", "money"), Col("GST (included)", "gst", "money"),
        Col("Net sales", "net", "money"), Col("Refunds", "refund", "money"), Col("Net after refunds", "after", "money"),
    ]
    return Report(cols, records, p.chips(("party", "user")),
                  totals=("bills", "sub", "disc", "gst", "net", "refund", "after"))


# ---------- 2. Sales by product ----------
@register("Sales", "Sales by product", uses=("product", "group", "party", "user"))
def sales_by_product(p):
    items = SaleItem.objects.filter(sale__in=_sales(p))
    rets = SaleReturnItem.objects.filter(
        sale_return__created_at__date__gte=p.date_from, sale_return__created_at__date__lte=p.date_to
    )
    if p.party:
        rets = rets.filter(sale_return__sale__customer_id=p.party)
    if p.user:
        rets = rets.filter(sale_return__processed_by_id=p.user)
    if p.product:
        items, rets = items.filter(product_id=p.product), rets.filter(product_id=p.product)
    if p.group:
        items, rets = items.filter(product__category_id=p.group), rets.filter(product__category_id=p.group)

    sold = items.order_by().values("product_id").annotate(
        t_name=Max("product_name"), t_code=Max("product_code"), t_qty=Sum("quantity"),
        t_amt=Sum("line_total"), t_profit=Sum(PROFIT_SOLD),
    )
    back = rets.order_by().values("product_id").annotate(
        t_name=Max("product_name"), t_qty=Sum("quantity"), t_amt=Sum("refund_amount"), t_profit=Sum(PROFIT_BACK),
    )

    rows = {}
    for r in sold:
        rows[r["product_id"]] = {
            "name": r["t_name"], "code": r["t_code"] or "", "qty": r["t_qty"], "ret_qty": ZERO,
            "amount": r["t_amt"], "refund": ZERO, "profit": r["t_profit"],
        }
    for r in back:
        x = rows.setdefault(r["product_id"], {
            "name": r["t_name"], "code": "", "qty": ZERO, "ret_qty": ZERO, "amount": ZERO, "refund": ZERO, "profit": ZERO,
        })
        x["ret_qty"], x["refund"], x["profit"] = r["t_qty"], r["t_amt"], x["profit"] - r["t_profit"]
    records = sorted(rows.values(), key=lambda x: x["amount"], reverse=True)

    cols = [
        Col("Code", "code"), Col("Product", "name"), Col("Qty sold", "qty", "qty"), Col("Qty returned", "ret_qty", "qty"),
        Col("Sales", "amount", "money"), Col("Refunds", "refund", "money"), Col("Gross profit", "profit", "money"),
    ]
    notes = ["Gross profit = sales without GST, minus the cost price, minus returns. It is before expenses."]
    return Report(cols, records, p.chips(("product", "group", "party", "user")),
                  totals=("qty", "ret_qty", "amount", "refund", "profit"), notes=notes)


# ---------- 3. Sales by payment type ----------
@register("Sales", "Sales by payment type", uses=("party", "user"))
def sales_by_payment(p):
    labels = dict(Payment.Method.choices)
    order = [m for m, _ in Payment.Method.choices]

    got = {
        r["method"]: r for r in Payment.objects.filter(sale__in=_sales(p)).order_by().values("method")
        .annotate(t_n=Count("sale_id", distinct=True), t_amt=Sum("amount"))
    }
    gave = {
        r["refund_method"]: r for r in _returns(p).order_by().values("refund_method")
        .annotate(t_n=Count("id"), t_amt=Sum("refund_total"))
    }

    records, total_in, total_out = [], ZERO, ZERO
    for m in order:
        if m in got:
            records.append({"type": "Received", "method": labels[m], "n": got[m]["t_n"], "amt": got[m]["t_amt"]})
            total_in += got[m]["t_amt"]
    for m in order:
        if m in gave:
            records.append({"type": "Refunded", "method": labels[m], "n": gave[m]["t_n"], "amt": gave[m]["t_amt"]})
            total_out += gave[m]["t_amt"]
    if records:
        records += [
            {"type": "Total received", "method": "", "n": None, "amt": total_in},
            {"type": "Total refunded", "method": "", "n": None, "amt": total_out},
            {"type": "Net", "method": "", "n": None, "amt": total_in - total_out},
        ]

    cols = [Col("Type", "type"), Col("Method", "method"), Col("Bills / returns", "n", "int"), Col("Amount", "amt", "money")]
    notes = ["Cash is shown after change was handed back, so received totals match the bill totals."]
    return Report(cols, records, p.chips(("party", "user")), notes=notes)


# ---------- 4. Bill-wise sales with GST ----------
@register("Sales", "Bill-wise sales with GST", uses=("party", "user"))
def bill_wise_gst(p):
    sales = _sales(p)
    split = defaultdict(lambda: [ZERO, ZERO])
    for (sale_id, rate), (_, tax) in _rate_groups(sales).items():
        if rate > 0:
            cgst, sgst = split_tax(tax)       # the same function the invoice page uses
            split[sale_id][0] += cgst
            split[sale_id][1] += sgst

    records = []
    for s in sales.select_related("customer").order_by("created_at", "id"):
        records.append({
            "invoice": s.invoice_no, "day": _local_date(s.created_at), "customer": s.customer.name,
            "gstin": "" if s.customer.is_system else s.customer.gstin,
            "taxable": s.total - s.tax_total, "cgst": split[s.pk][0], "sgst": split[s.pk][1],
            "gst": s.tax_total, "total": s.total,
        })
    cols = [
        Col("Invoice", "invoice"), Col("Date", "day", "date"), Col("Customer", "customer"), Col("Customer GSTIN", "gstin"),
        Col("Taxable value", "taxable", "money"), Col("CGST", "cgst", "money"), Col("SGST", "sgst", "money"),
        Col("GST total", "gst", "money"), Col("Bill total", "total", "money"),
    ]
    notes = ["Before returns (see Sales returns). GST is split half and half, which assumes the customer is in the same state."]
    return Report(cols, records, p.chips(("party", "user")),
                  totals=("taxable", "cgst", "sgst", "gst", "total"), notes=notes)


# ---------- 5. GST summary by rate ----------
@register("Sales", "GST summary by rate", uses=("party", "user"))
def gst_summary(p):
    by_rate = defaultdict(lambda: {"bills": set(), "taxable": ZERO, "cgst": ZERO, "sgst": ZERO, "gst": ZERO})
    for (sale_id, rate), (taxable, tax) in _rate_groups(_sales(p)).items():
        r = by_rate[rate]
        r["bills"].add(sale_id)
        r["taxable"] += taxable
        r["gst"] += tax
        if rate > 0:
            cgst, sgst = split_tax(tax)
            r["cgst"] += cgst
            r["sgst"] += sgst
    records = [
        {"rate": rate, "bills": len(v["bills"]), "taxable": v["taxable"], "cgst": v["cgst"], "sgst": v["sgst"], "gst": v["gst"]}
        for rate, v in sorted(by_rate.items())
    ]
    cols = [
        Col("GST rate", "rate", "pct"), Col("Bills", "bills", "int"), Col("Taxable value", "taxable", "money"),
        Col("CGST", "cgst", "money"), Col("SGST", "sgst", "money"), Col("GST total", "gst", "money"),
    ]
    notes = ["Before returns (see Sales returns). A bill with items at two rates is counted once under each rate."]
    return Report(cols, records, p.chips(("party", "user")), totals=("taxable", "cgst", "sgst", "gst"), notes=notes)


# ---------- 6. Sales returns ----------
@register("Sales", "Sales returns", uses=("party", "user"))
def sales_returns(p):
    records = [
        {
            "no": r.return_no, "when": r.created_at, "invoice": r.sale.invoice_no, "customer": r.sale.customer.name,
            "method": r.get_refund_method_display(), "refund": r.refund_total, "gst": r.tax_total,
            "by": r.processed_by.get_full_name() or r.processed_by.username, "reason": r.reason,
        }
        for r in _returns(p).select_related("sale", "sale__customer", "processed_by").order_by("created_at", "id")
    ]
    cols = [
        Col("Return", "no"), Col("Date and time", "when", "datetime"), Col("Invoice", "invoice"), Col("Customer", "customer"),
        Col("Refund by", "method"), Col("Refund", "refund", "money"), Col("GST in refund", "gst", "money"),
        Col("By", "by"), Col("Reason", "reason"),
    ]
    return Report(cols, records, p.chips(("party", "user")), totals=("refund", "gst"))


# ---------- 7. Voided items ----------
@register("Sales", "Voided items", uses=("party", "user"))
def voided_bills(p):
    qs = Sale.objects.filter(
        status=Sale.Status.VOIDED, voided_at__date__gte=p.date_from, voided_at__date__lte=p.date_to
    )
    if p.party:
        qs = qs.filter(customer_id=p.party)
    if p.user:
        qs = qs.filter(cashier_id=p.user)
    records = [
        {
            "invoice": s.invoice_no, "day": _local_date(s.created_at), "when": s.voided_at, "customer": s.customer.name,
            "by": (s.voided_by.get_full_name() or s.voided_by.username) if s.voided_by else "",
            "total": s.total, "reason": s.void_reason,
        }
        for s in qs.select_related("customer", "voided_by").order_by("voided_at", "id")
    ]
    cols = [
        Col("Invoice", "invoice"), Col("Bill date", "day", "date"), Col("Voided at", "when", "datetime"),
        Col("Customer", "customer"), Col("Voided by", "by"), Col("Bill total", "total", "money"), Col("Reason", "reason"),
    ]
    notes = ["The date filter applies to the day the bill was voided. User filters by the cashier who made the bill."]
    return Report(cols, records, p.chips(("party", "user")), totals=("total",), notes=notes)


# ---------- 8. Discounts granted ----------
@register("Sales", "Discounts granted", uses=("party", "user"))
def discounts_granted(p):
    records = []
    for s in _sales(p).filter(discount_total__gt=0).select_related("customer", "cashier").order_by("created_at", "id"):
        records.append({
            "invoice": s.invoice_no, "day": _local_date(s.created_at), "customer": s.customer.name,
            "cashier": s.cashier.get_full_name() or s.cashier.username, "sub": s.subtotal,
            "disc": s.discount_total, "pct": (s.discount_total / s.subtotal * 100) if s.subtotal else ZERO, "total": s.total,
        })
    cols = [
        Col("Invoice", "invoice"), Col("Date", "day", "date"), Col("Customer", "customer"), Col("Cashier", "cashier"),
        Col("Subtotal", "sub", "money"), Col("Discount", "disc", "money"), Col("Discount %", "pct", "pct"), Col("Total", "total", "money"),
    ]
    return Report(cols, records, p.chips(("party", "user")), totals=("sub", "disc", "total"))


# ---------- 9. Items discounts ----------
@register("Sales", "Items discounts", uses=("product", "group", "party", "user"))
def items_discounts(p):
    qs = SaleItem.objects.filter(sale__in=_sales(p), discount__gt=0)
    if p.product:
        qs = qs.filter(product_id=p.product)
    if p.group:
        qs = qs.filter(product__category_id=p.group)
    records = []
    for it in qs.select_related("sale", "sale__cashier").order_by("sale__created_at", "id"):
        gross = it.line_total + it.discount
        records.append({
            "invoice": it.sale.invoice_no, "day": _local_date(it.sale.created_at), "product": it.product_name,
            "qty": it.quantity, "gross": gross, "disc": it.discount,
            "pct": (it.discount / gross * 100) if gross else ZERO, "total": it.line_total,
            "cashier": it.sale.cashier.get_full_name() or it.sale.cashier.username,
        })
    cols = [
        Col("Invoice", "invoice"), Col("Date", "day", "date"), Col("Product", "product"), Col("Qty", "qty", "qty"),
        Col("Before discount", "gross", "money"), Col("Discount", "disc", "money"), Col("Discount %", "pct", "pct"),
        Col("Line total", "total", "money"), Col("Cashier", "cashier"),
    ]
    notes = ["The discount on a line includes that line's share of any whole-bill discount."]
    return Report(cols, records, p.chips(("product", "group", "party", "user")), totals=("gross", "disc", "total"), notes=notes)