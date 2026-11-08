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
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from inventory.stock import low_stock, qty_fmt, with_stock
from expenses.models import Expense
from people.models import Contact
from pos.models import Sale, SaleItem, SaleReturn, SaleReturnItem
from purchase.models import Purchase

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


def _chart_money(value):
    return f"{Decimal(value or 0):.2f}"


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


def _sales_statistics(key, start, end):
    tz = timezone.get_current_timezone()
    if key == "today":
        hours = range(24)
        revenue = {hour: ZERO for hour in hours}
        expenses = {hour: ZERO for hour in hours}
        for row in (
            _sales_in(start, end).order_by()
            .annotate(bucket=TruncHour("created_at", tzinfo=tz))
            .values("bucket")
            .annotate(total=Sum("total"))
        ):
            revenue[timezone.localtime(row["bucket"], tz).hour] += row["total"] or ZERO
        for row in (
            SaleReturn.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
            .order_by()
            .annotate(bucket=TruncHour("created_at", tzinfo=tz))
            .values("bucket")
            .annotate(total=Sum("refund_total"))
        ):
            revenue[timezone.localtime(row["bucket"], tz).hour] -= row["total"] or ZERO
        for row in (
            Expense.objects.filter(
                status=Expense.Status.ACTIVE, created_at__date__gte=start, created_at__date__lte=end
            )
            .order_by()
            .annotate(bucket=TruncHour("created_at", tzinfo=tz))
            .values("bucket")
            .annotate(total=Sum("amount"))
        ):
            expenses[timezone.localtime(row["bucket"], tz).hour] += row["total"] or ZERO

        labels = [f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}" for hour in hours]
        tooltips = [f"{start:%A, %d %B %Y} · {label}" for label in labels]
        return {
            "labels": labels,
            "tooltips": tooltips,
            "revenue": [_chart_money(revenue[hour]) for hour in hours],
            "expenses": [_chart_money(expenses[hour]) for hour in hours],
            "period": f"Today · {start:%d-%m-%Y}",
        }

    if key == "year":
        buckets = range(1, end.month + 1)
        revenue = {month: ZERO for month in buckets}
        expenses = {month: ZERO for month in buckets}
        for row in (
            _sales_in(start, end).order_by()
            .annotate(bucket=TruncMonth("created_at", tzinfo=tz))
            .values("bucket")
            .annotate(total=Sum("total"))
        ):
            revenue[timezone.localtime(row["bucket"], tz).month] += row["total"] or ZERO
        for row in (
            SaleReturn.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
            .order_by()
            .annotate(bucket=TruncMonth("created_at", tzinfo=tz))
            .values("bucket")
            .annotate(total=Sum("refund_total"))
        ):
            revenue[timezone.localtime(row["bucket"], tz).month] -= row["total"] or ZERO
        for row in (
            Expense.objects.filter(
                status=Expense.Status.ACTIVE, expense_date__gte=start, expense_date__lte=end
            )
            .order_by()
            .values("expense_date__month")
            .annotate(total=Sum("amount"))
        ):
            expenses[row["expense_date__month"]] += row["total"] or ZERO
        labels = [calendar.month_abbr[month] for month in buckets]
        tooltips = [f"{calendar.month_name[month]} {end.year}" for month in buckets]
    else:
        days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
        revenue = {day: ZERO for day in days}
        expenses = {day: ZERO for day in days}
        for row in (
            _sales_in(start, end).order_by()
            .annotate(bucket=TruncDate("created_at", tzinfo=tz))
            .values("bucket")
            .annotate(total=Sum("total"))
        ):
            revenue[row["bucket"]] += row["total"] or ZERO
        for row in (
            SaleReturn.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
            .order_by()
            .annotate(bucket=TruncDate("created_at", tzinfo=tz))
            .values("bucket")
            .annotate(total=Sum("refund_total"))
        ):
            revenue[row["bucket"]] -= row["total"] or ZERO
        for row in (
            Expense.objects.filter(
                status=Expense.Status.ACTIVE, expense_date__gte=start, expense_date__lte=end
            )
            .order_by()
            .values("expense_date")
            .annotate(total=Sum("amount"))
        ):
            expenses[row["expense_date"]] += row["total"] or ZERO
        labels = [
            day.strftime("%a %d %b") if key == "week" else day.strftime("%d %b")
            for day in days
        ]
        tooltips = [day.strftime("%A, %d %B %Y") for day in days]

    chart = {
        "labels": labels,
        "tooltips": tooltips,
        "period": f"{dict(RANGES)[key]} · {start:%d-%m-%Y}" + (
            "" if start == end else f" to {end:%d-%m-%Y}"
        ),
    }
    if key == "year":
        chart["revenue"] = [_chart_money(revenue[month]) for month in buckets]
        chart["expenses"] = [_chart_money(expenses[month]) for month in buckets]
    else:
        chart["revenue"] = [_chart_money(revenue[day]) for day in days]
        chart["expenses"] = [_chart_money(expenses[day]) for day in days]
    return chart


@login_required
def sales_statistics(request):
    if not request.user.is_admin_role:
        return JsonResponse({"error": "Admin access is required."}, status=403)
    key = request.GET.get("range", "today")
    if key not in dict(RANGES):
        return JsonResponse({"error": "Choose a valid date range."}, status=400)
    today = timezone.localdate()
    (start, end), _ = period_bounds(key, today)
    return JsonResponse(_sales_statistics(key, start, end))


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
    current_expenses = Expense.objects.filter(
        status=Expense.Status.ACTIVE, expense_date__gte=start, expense_date__lte=end
    ).aggregate(total=_sum("amount"))["total"]
    previous_expenses = Expense.objects.filter(
        status=Expense.Status.ACTIVE, expense_date__gte=p_start, expense_date__lte=p_end
    ).aggregate(total=_sum("amount"))["total"]

    purchase_total = Purchase.objects.filter(
        status=Purchase.Status.COMPLETED,
        purchase_date__gte=start,
        purchase_date__lte=end,
    ).aggregate(total=Coalesce(Sum("grand_total"), Value(ZERO), output_field=MONEY_FIELD))["total"]
    prev_purchase_total = Purchase.objects.filter(
        status=Purchase.Status.COMPLETED,
        purchase_date__gte=p_start,
        purchase_date__lte=p_end,
    ).aggregate(total=Coalesce(Sum("grand_total"), Value(ZERO), output_field=MONEY_FIELD))["total"]

    primary_cards = [
        {"label": "Total Sales", "display": inr(cur["sales"]), "icon": "bi-receipt",
         "change": _change(cur["sales"], prev["sales"], label)},
        {"label": "Sales Return", "display": inr(cur["returns"]), "icon": "bi-arrow-repeat",
         "change": _change(cur["returns"], prev["returns"], label)},
        {"label": "Total Purchase", "display": inr(purchase_total) if purchase_total else "\u2014", "icon": "bi-bag",
         "change": _change(purchase_total, prev_purchase_total, label) if purchase_total or prev_purchase_total else None},
        {"label": "Purchase Return", "display": "\u2014", "icon": "bi-shield-check"},
    ]
    secondary_cards = [
        {"label": "Gross Profit", "display": inr(cur["profit"]), "icon": "bi-layers",
         "change": _change(cur["profit"], prev["profit"], label), "foot": "Before expenses"},
        # Purchases are not expenses: product costs are already included in Gross Profit.
        {"label": "Net Profit", "display": inr(cur["profit"] - current_expenses), "icon": "bi-graph-down-arrow",
         "change": _change(cur["profit"] - current_expenses, prev["profit"] - previous_expenses, label)},
        {"label": "Total Expenses", "display": inr(current_expenses), "icon": "bi-wallet2",
         "change": _change(current_expenses, previous_expenses, label)},
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
            "cats": {"labels": [c["name"] for c in categories], "values": [_chart_money(r["amount"]) for r in cat_rows]},
            "sales_statistics": _sales_statistics(key, start, end),
        },
    }
    return render(request, "dashboard/home.html", context)