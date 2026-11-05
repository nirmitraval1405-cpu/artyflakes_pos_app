from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from inventory.stock import low_stock, qty_fmt, with_stock
from people.models import Contact

ZERO = Decimal("0.00")


@login_required
def home(request):
    # TODO: replace each ZERO with a real aggregate when its model exists:
    #   Total Sales / Sales Due   -> Phase 5-6 (Sale, Payment)
    #   Total Purchase            -> Phase 7 (Purchase)
    #   Total Expenses            -> Phase 8 (Expense)
    primary_cards = [
        {"label": "Total Sales", "value": ZERO, "icon": "bi-receipt", "color": "orange"},
        {"label": "Total Sales Return", "value": ZERO, "icon": "bi-arrow-repeat", "color": "navy"},
        {"label": "Total Purchase", "value": ZERO, "icon": "bi-bag", "color": "teal"},
        {"label": "Total Purchase Return", "value": ZERO, "icon": "bi-shield-check", "color": "blue"},
    ]
    secondary_cards = [
        {"label": "Profit", "value": ZERO, "icon": "bi-layers", "tone": "teal"},
        {"label": "Total Sales Due", "value": ZERO, "icon": "bi-clock-history", "tone": "orange"},
        {"label": "Total Expenses", "value": ZERO, "icon": "bi-wallet2", "tone": "red"},
        {"label": "Total Payment Returns", "value": ZERO, "icon": "bi-cash-coin", "tone": "blue"},
    ]

    low = low_stock(with_stock()).order_by("stock", "name")[:5]
    low_rows = [
        {
            "name": p.name,
            "code": p.code or "",
            "stock": qty_fmt(p.stock),
            "out": p.stock <= 0,
        }
        for p in low
    ]

    context = {
        "today": timezone.localdate(),
        "primary_cards": primary_cards,
        "secondary_cards": secondary_cards,
        "overall": {
            "suppliers": Contact.objects.filter(is_supplier=True, is_active=True).count(),
            "customers": Contact.objects.filter(is_customer=True, is_system=False, is_active=True).count(),
        },
        "low_stock": low_rows,
    }
    return render(request, "dashboard/home.html", context)