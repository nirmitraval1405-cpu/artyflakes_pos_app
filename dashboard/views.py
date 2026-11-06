import calendar
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import (
    CharField, Count, DecimalField, ExpressionWrapper, F, Max, Sum, Value,
)
from django.db.models.functions import (
    Coalesce, ExtractHour, ExtractWeekDay, TruncDate, TruncHour, TruncMonth,
)
from django.shortcuts import redirect, render
from django.utils import timezone

from inventory.stock import low_stock, qty_fmt, with_stock
from people.models import Contact
from pos.models import Sale, SaleItem, SaleReturn, SaleReturnItem

ZERO = Decimal("0")
MONEY_FIELD = DecimalField(max_digits=18, decimal_places=2)
PROFIT_FIELD = DecimalField(max_digits=24, decimal_places=5)

# Revenue without GST minus what the goods cost. Returns reverse the same calculation.
PROFIT_SOLD = ExpressionWrapper(
    F("line_total") - F("tax_amount") - F("cost_price") * F("quantity"), output_field=PROFIT_FIELD
)
PROFIT_BACK = ExpressionWrapper(
    F("refund_amount") - F("tax_amount") - F("sale_item__cost_price") * F("quantity"), output_field=PROFIT_FIELD
)

RANGES = [
    ("today", "Today"),
    ("week", "Last 7 days"),
    ("days30", "Last 30 days"),
    ("month", "This month"),
    ("year", "This year"),
]
PREV_LABEL = {
    "today": "yesterday",
    "week": "previous 7 days",
    "days30": "previous 30 days",
    "month": "same days last month",
    "year": "same days last year",
}


# ---------- Helpers ----------
def inr(value):
    """12345678.5 -> '₹1,23,45,678.50' (Indian digit grouping)."""
    value = Decimal(value).quantize(Decimal("0.01"))
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{sign}\u20b9{whole}.{frac}"


def _shift_year(d, years):
    try:
        return d.replace(year=d.year + years)
    except ValueError:                      # 29 February
        return d.replace(year=d.year + years, day=28)


def period_bounds(key, today):
    """Returns ((start, end), (prev_start, prev_end)) as dates, both inclusive."""
    one = timedelta(days=1)
    if key == "today":
        return (today, today), (today - one, today - one)
    if key == "week":
        return (today - 6 * one, today), (today - 13 * one, today - 7 * one)
    if key == "days30":
        return (today - 29 * one, today), (today - 59 * one, today - 30 * one)
    if key == "month":
        start = today.replace(day=1)
        prev_end_full = start - one
        prev_start = prev_end_full.replace(day=1)
        prev_end = min(prev_start + (today - start), prev_end_full)    # same number of days
        return (start, today), (prev_start, prev_end)
    start = today.replace(month=1, day=1)                              # year
    return (start, today), (_shift_year(start, -1), _shift_year(today, -1))


def _change(current, previous, label):
    current, previous = Decimal(current), Decimal(previous)
    if previous == 0:
        if current == 0:
            return {"dir": "flat", "text": f"No change vs {label}"}
        return {"dir": "up", "text": f"New vs {label}"}
    pct = ((current - previous) / abs(previous) * 100).quantize(Decimal("0.1"))
    if pct == 0:
        return {"dir": "flat", "text": f"No change vs {label}"}
    return {"dir": "up" if pct > 0 else "down", "text": f"{pct:+.1f}% vs {label}"}


def _sum(field):
    return Coalesce(Sum(field), Value(ZERO), output_field=MONEY_FIELD)


def _sales_in(start, end):
    return Sale.objects.filter(
        status=Sale.Status.COMPLETED, created_at__date__gte=start, created_at__date__lte=end
    )


def _metrics(start, end):
    sales = _sales_in(start, end)
    agg = sales.aggregate(total=_sum("total"), bills=Count("id"))
    sold = SaleItem.objects.filter(sale__in=sales).aggregate(
        p=Coalesce(Sum(PROFIT_SOLD), Value(ZERO), output_field=PROFIT_FIELD)
    )["p"]

    returns = SaleReturn.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
    returned = returns.aggregate(t=_sum("refund_total"))["t"]
    back = SaleReturnItem.objects.filter(sale_return__in=returns).aggregate(
        p=Coalesce(Sum(PROFIT_BACK), Value(ZERO), output_field=PROFIT_FIELD)
    )["p"]

    return {
        "sales": agg["total"],
        "bills": agg["bills"],
        "returns": returned,
        "profit": (sold - back).quantize(Decimal("0.01")),
    }


def _sales_chart(key, start, end):
    qs = _sales_in(start, end).order_by()
    if key == "today":
        rows = qs.annotate(b=TruncHour("created_at")).values("b").annotate(t=Sum("total"))
        by = {timezone.localtime(r["b"]).hour: r["t"] for r in rows}
        labels = [f"{h:02d}:00" for h in range(24)]
        values = [float(by.get(h, 0)) for h in range(24)]
    elif key == "year":
        rows = qs.annotate(b=TruncMonth("created_at")).values("b").annotate(t=Sum("total"))
        by = {timezone.localtime(r["b"]).month: r["t"] for r in rows}
        months = range(1, end.month + 1)
        labels = [calendar.month_abbr[m] for m in months]
        values = [float(by.get(m, 0)) for m in months]
    else:
        rows = qs.annotate(b=TruncDate("created_at")).values("b").annotate(t=Sum("total"))
        by = {r["b"]: r["t"] for r in rows}
        dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        labels = [d.strftime("%d %b") for d in dates]
        values = [float(by.get(d, 0)) for d in dates]
    return {"labels": labels, "values": values}


HEAT_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
HEAT_ROWS = ["12\u20133 AM", "3\u20136 AM", "6\u20139 AM", "9 AM\u201312 PM",
             "12\u20133 PM", "3\u20136 PM", "6\u20139 PM", "9 PM\u201312 AM"]


def _heatmap(today):
    since = today - timedelta(days=89)
    rows = (
        Sale.objects.filter(status=Sale.Status.COMPLETED, created_at__date__gte=since)
        .order_by()
        .annotate(wd=ExtractWeekDay("created_at"), hr=ExtractHour("created_at"))
        .values("wd", "hr")
        .annotate(n=Count("id"))
    )
    grid = [[0] * 7 for _ in range(8)]
    for r in rows:
        day = (r["wd"] + 5) % 7            # Django: Sunday=1 ... Saturday=7  ->  Monday=0 ... Sunday=6
        grid[r["hr"] // 3][day] += r["n"]

    peak = max((n for row in grid for n in row), default=0)
    total = sum(n for row in grid for n in row)
    table = []
    for label, row in zip(HEAT_ROWS, grid):
        cells = [
            {"n": n, "alpha": f"{(0.15 + 0.85 * n / peak) if n else 0.05:.2f}"} for n in row
        ]
        table.append({"label": label, "cells": cells})
    return {"days": HEAT_DAYS, "rows": table, "total": total}


# ---------- The page ----------
@login_required
def home(request):
    # The dashboard shows profit and other business figures, so it is for admins only.
    if not request.user.is_admin_role:
        return redirect("pos:screen")

    today = timezone.localdate()
    key = request.GET.get("range", "today")
    if key not in dict(RANGES):
        key = "today"
    (start, end), (p_start, p_end) = period_bounds(key, today)
    label = PREV_LABEL[key]

    cur = _metrics(start, end)
    prev = _metrics(p_start, p_end)

    primary_cards = [
        {"label": "Total Sales", "display": inr(cur["sales"]), "icon": "bi-receipt",
         "change": _change(cur["sales"], prev["sales"], label)},
        {"label": "Sales Return", "display": inr(cur["returns"]), "icon": "bi-arrow-repeat",
         "change": _change(cur["returns"], prev["returns"], label)},
        {"label": "Total Purchase", "display": "\u2014", "icon": "bi-bag"},
        {"label": "Purchase Return", "display": "\u2014", "icon": "bi-shield-check"},
    ]
    secondary_cards = [
        {"label": "Gross Profit", "display": inr(cur["profit"]), "icon": "bi-layers",
         "change": _change(cur["profit"], prev["profit"], label), "foot": "Before expenses"},
        {"label": "Total Sales Due", "display": "\u2014", "icon": "bi-clock-history"},
        {"label": "Total Expenses", "display": "\u2014", "icon": "bi-wallet2"},
        {"label": "Bills Made", "display": str(cur["bills"]), "icon": "bi-receipt-cutoff",
         "change": _change(cur["bills"], prev["bills"], label)},
    ]

    sales = _sales_in(start, end)
    items = SaleItem.objects.filter(sale__in=sales).order_by()

    top_products = [
        {"name": r["name"], "code": r["code"], "qty": qty_fmt(r["qty"]), "amount": inr(r["amount"])}
        for r in items.values("product_id")
        .annotate(name=Max("product_name"), code=Max("product_code"), qty=Sum("quantity"), amount=Sum("line_total"))
        .order_by("-amount")[:5]
    ]

    top_customers = [
        {"name": r["name"], "orders": r["orders"], "amount": inr(r["amount"])}
        for r in sales.exclude(customer__is_system=True).order_by()
        .values("customer_id")
        .annotate(name=Max("customer__name"), orders=Count("id"), amount=Sum("total"))
        .order_by("-amount")[:5]
    ]

    cat_rows = list(
        items.annotate(cat=Coalesce("product__category__name", Value("No group"), output_field=CharField()))
        .values("cat")
        .annotate(amount=Sum("line_total"), qty=Sum("quantity"))
        .order_by("-amount")[:6]
    )
    categories = [{"name": r["cat"], "amount": inr(r["amount"]), "qty": qty_fmt(r["qty"])} for r in cat_rows]

    # Customers who bought in this period: new, or seen before?
    buyers = set(
        sales.exclude(customer__is_system=True).order_by().values_list("customer_id", flat=True).distinct()
    )
    seen_before = set(
        Sale.objects.filter(status=Sale.Status.COMPLETED, created_at__date__lt=start, customer_id__in=buyers)
        .order_by().values_list("customer_id", flat=True).distinct()
    )

    low = low_stock(with_stock()).order_by("stock", "name")[:5]
    low_rows = [
        {"name": p.name, "code": p.code or "", "stock": qty_fmt(p.stock), "out": p.stock <= 0}
        for p in low
    ]

    recent = Sale.objects.select_related("customer").order_by("-created_at")[:5]

    context = {
        "today": today,
        "ranges": RANGES,
        "range_key": key,
        "range_label": dict(RANGES)[key],
        "period_text": f"{start:%d-%m-%Y}" + ("" if start == end else f" to {end:%d-%m-%Y}"),
        "primary_cards": primary_cards,
        "secondary_cards": secondary_cards,
        "overall": {
            "suppliers": Contact.objects.filter(is_supplier=True, is_active=True).count(),
            "customers": Contact.objects.filter(is_customer=True, is_system=False, is_active=True).count(),
            "orders": Sale.objects.filter(status=Sale.Status.COMPLETED).count(),
            "returning": len(seen_before),
            "first_time": len(buyers) - len(seen_before),
        },
        "top_products": top_products,
        "top_customers": top_customers,
        "categories": categories,
        "low_stock": low_rows,
        "recent_sales": recent,
        "heat": _heatmap(today),
        "chart_data": {
            "sales": _sales_chart(key, start, end),
            "cats": {"labels": [c["name"] for c in categories], "values": [float(r["amount"]) for r in cat_rows]},
        },
    }
    return render(request, "dashboard/home.html", context)