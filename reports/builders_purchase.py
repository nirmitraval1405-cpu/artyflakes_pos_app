from decimal import Decimal

from django.db.models import Count, Max, Sum
from django.utils import timezone

from purchase.models import Purchase, PurchaseItem, PurchasePayment

from .helpers import Col, Report
from .registry import register

ZERO = Decimal("0")


def _purchases(p, *, include_cancelled=False):
    purchases = Purchase.objects.select_related("supplier").filter(
        purchase_date__gte=p.date_from,
        purchase_date__lte=p.date_to,
    )
    if not include_cancelled:
        purchases = purchases.filter(status=Purchase.Status.COMPLETED)
    if p.party:
        purchases = purchases.filter(supplier_id=p.party)
    return purchases


def _items(p):
    items = PurchaseItem.objects.filter(
        purchase__purchase_date__gte=p.date_from,
        purchase__purchase_date__lte=p.date_to,
        purchase__status=Purchase.Status.COMPLETED,
    )
    if p.party:
        items = items.filter(purchase__supplier_id=p.party)
    if p.product:
        items = items.filter(product_id=p.product)
    if p.group:
        items = items.filter(product__category_id=p.group)
    return items


@register("Purchase", "Products", uses=("product", "group", "party"))
def products(p):
    rows = (
        _items(p).order_by().values("product_id")
        .annotate(
            name=Max("product_name"),
            code=Max("product_code"),
            qty=Sum("quantity"),
            total=Sum("taxable_amount"),
            discount=Sum("discount"),
        )
        .order_by("name")
    )
    records = [
        {
            "code": row["code"] or "",
            "name": row["name"],
            "qty": row["qty"],
            "avg": row["total"] / row["qty"] if row["qty"] else ZERO,
            "total": row["total"],
            "disc": row["discount"],
        }
        for row in rows
    ]
    cols = [
        Col("Code", "code"), Col("Product", "name"), Col("Quantity", "qty", "qty"),
        Col("Average cost", "avg", "money"), Col("Taxable amount", "total", "money"),
        Col("Discount", "disc", "money"),
    ]
    return Report(cols, records, p.chips(("product", "group", "party")), totals=("qty", "total", "disc"))


@register("Purchase", "Suppliers", uses=("party",))
def suppliers(p):
    rows = (
        _purchases(p).order_by().values("supplier_id")
        .annotate(
            name=Max("supplier__name"),
            invoices=Count("id"),
            total=Sum("grand_total"),
            paid=Sum("amount_paid"),
            out=Sum("outstanding_amount"),
        )
        .order_by("name")
    )
    records = [
        {
            "name": row["name"], "invoices": row["invoices"], "total": row["total"],
            "paid": row["paid"], "out": row["out"],
        }
        for row in rows
    ]
    cols = [
        Col("Supplier", "name"), Col("Purchases", "invoices", "int"),
        Col("Total", "total", "money"), Col("Paid", "paid", "money"),
        Col("Outstanding", "out", "money"),
    ]
    return Report(cols, records, p.chips(("party",)), totals=("invoices", "total", "paid", "out"))


@register("Purchase", "Payment types", uses=("party",))
def payment_types(p):
    payments = PurchasePayment.objects.filter(
        payment_date__gte=p.date_from,
        payment_date__lte=p.date_to,
    )
    if p.party:
        payments = payments.filter(purchase__supplier_id=p.party)
    rows = (
        payments.order_by().values("method")
        .annotate(n=Count("id"), amt=Sum("amount"))
        .order_by("method")
    )
    records = []
    for row in rows:
        cancelled = payments.filter(
            method=row["method"], purchase__status=Purchase.Status.CANCELLED
        ).aggregate(total=Sum("amount"))["total"] or ZERO
        records.append({
            "method": dict(PurchasePayment.Method.choices).get(row["method"], row["method"]),
            "n": row["n"],
            "amt": row["amt"],
            "cx": cancelled,
        })
    cols = [
        Col("Payment method", "method"), Col("Payments", "n", "int"),
        Col("Amount paid", "amt", "money"), Col("Cancelled purchase payments", "cx", "money"),
    ]
    return Report(cols, records, p.chips(("party",)), totals=("n", "amt", "cx"))


@register("Purchase", "Payment types by supplier", uses=("party",))
def payment_types_by_supplier(p):
    rows = (
        PurchasePayment.objects.filter(
            payment_date__gte=p.date_from,
            payment_date__lte=p.date_to,
        )
        .order_by()
        .values("purchase__supplier_id", "purchase__supplier__name", "method")
        .annotate(n=Count("id"), amt=Sum("amount"))
        .order_by("purchase__supplier__name", "method")
    )
    if p.party:
        rows = rows.filter(purchase__supplier_id=p.party)
    labels = dict(PurchasePayment.Method.choices)
    records = [
        {
            "name": row["purchase__supplier__name"],
            "method": labels.get(row["method"], row["method"]),
            "n": row["n"],
            "amt": row["amt"],
        }
        for row in rows
    ]
    cols = [
        Col("Supplier", "name"), Col("Payment method", "method"),
        Col("Payments", "n", "int"), Col("Amount paid", "amt", "money"),
    ]
    return Report(cols, records, p.chips(("party",)), totals=("n", "amt"))


@register("Purchase", "Unpaid purchase", uses=("party",))
def unpaid_purchase(p):
    purchases = Purchase.objects.select_related("supplier").filter(
        status=Purchase.Status.COMPLETED,
        outstanding_amount__gt=ZERO,
    )
    if p.party:
        purchases = purchases.filter(supplier_id=p.party)
    purchases = purchases.order_by("purchase_date", "id")
    today = timezone.localdate()
    records = [
        {
            "purchase_no": purchase.purchase_no,
            "supplier": purchase.supplier.name,
            "date": purchase.purchase_date,
            "total": purchase.grand_total,
            "paid": purchase.amount_paid,
            "out": purchase.outstanding_amount,
            "days": max((today - purchase.purchase_date).days, 0),
        }
        for purchase in purchases
    ]
    cols = [
        Col("Purchase no.", "purchase_no"), Col("Supplier", "supplier"),
        Col("Purchase date", "date", "date"), Col("Total", "total", "money"),
        Col("Paid", "paid", "money"), Col("Outstanding", "out", "money"),
        Col("Days outstanding", "days", "int"),
    ]
    chips = p.chips(("party",), dates=False, party_label="Supplier")
    return Report(cols, records, chips, totals=("total", "paid", "out"))


@register("Purchase", "Purchase discounts", uses=("party",))
def purchase_discounts(p):
    records = [
        {
            "purchase_no": purchase.purchase_no,
            "supplier": purchase.supplier.name,
            "date": purchase.purchase_date,
            "subtotal": purchase.subtotal,
            "disc": purchase.discount_total,
        }
        for purchase in _purchases(p).filter(discount_total__gt=ZERO).order_by("purchase_date", "id")
    ]
    cols = [
        Col("Purchase no.", "purchase_no"), Col("Supplier", "supplier"),
        Col("Date", "date", "date"), Col("Subtotal", "subtotal", "money"),
        Col("Discount", "disc", "money"),
    ]
    return Report(cols, records, p.chips(("party",)), totals=("subtotal", "disc"))


@register("Purchase", "Purchased items discounts", uses=("product", "group", "party"))
def purchased_items_discounts(p):
    rows = (
        _items(p).filter(discount__gt=ZERO).order_by().values("product_id")
        .annotate(name=Max("product_name"), code=Max("product_code"), qty=Sum("quantity"), disc=Sum("discount"))
        .order_by("name")
    )
    records = [
        {"code": row["code"] or "", "name": row["name"], "qty": row["qty"], "disc": row["disc"]}
        for row in rows
    ]
    cols = [
        Col("Code", "code"), Col("Product", "name"), Col("Quantity", "qty", "qty"),
        Col("Discount", "disc", "money"),
    ]
    return Report(cols, records, p.chips(("product", "group", "party")), totals=("qty", "disc"))


@register("Purchase", "Purchase invoice list", uses=("party",))
def purchase_invoice_list(p):
    records = [
        {
            "purchase_no": purchase.purchase_no,
            "supplier_invoice": purchase.supplier_invoice_no,
            "supplier": purchase.supplier.name,
            "date": purchase.purchase_date,
            "status": purchase.get_status_display(),
            "total": purchase.grand_total,
            "paid": purchase.amount_paid,
            "out": purchase.outstanding_amount,
        }
        for purchase in _purchases(p, include_cancelled=True).order_by("purchase_date", "id")
    ]
    cols = [
        Col("Purchase no.", "purchase_no"), Col("Supplier invoice", "supplier_invoice"),
        Col("Supplier", "supplier"), Col("Date", "date", "date"),
        Col("Status", "status"), Col("Total", "total", "money"),
        Col("Paid", "paid", "money"), Col("Outstanding", "out", "money"),
    ]
    return Report(
        cols, records, p.chips(("party",)), totals=("total", "paid", "out"),
        totals_where=lambda row: row["status"] != Purchase.Status.CANCELLED.label,
    )


@register("Purchase", "Tax rates", uses=("product", "group", "party"))
def tax_rates(p):
    rows = (
        _items(p).order_by().values("gst_rate")
        .annotate(qty=Sum("quantity"), taxable=Sum("taxable_amount"), gst=Sum("gst_amount"))
        .order_by("gst_rate")
    )
    records = [
        {"rate": row["gst_rate"], "qty": row["qty"], "taxable": row["taxable"], "gst": row["gst"]}
        for row in rows
    ]
    cols = [
        Col("GST rate", "rate", "pct"), Col("Quantity", "qty", "qty"),
        Col("Taxable amount", "taxable", "money"), Col("GST", "gst", "money"),
    ]
    return Report(cols, records, p.chips(("product", "group", "party")), totals=("qty", "taxable", "gst"))


@register("Purchase", "Expiration date", uses=("product", "group", "party"))
def expiration_date(p):
    cols = [
        Col("Product", "name"), Col("Code", "code"), Col("Supplier", "supplier"),
        Col("Purchase date", "date", "date"), Col("Expiration date", "expiry", "date"),
    ]
    notes = ["Expiration dates are not currently recorded on purchase items."]
    return Report(cols, [], p.chips(("product", "group", "party")), notes=notes)
