import json
import logging
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from inventory.models import Product
from inventory.stock import with_stock
from people.models import Contact

from .services import CheckoutError, create_sale

logger = logging.getLogger(__name__)


@login_required
def screen(request):
    # Admins have no discount limit, so the page shows the limit to Cashiers only
    limit = "" if request.user.is_admin_role else str(getattr(settings, "POS_CASHIER_MAX_DISCOUNT_PERCENT", 10))
    return render(request, "pos/screen.html", {"max_discount": limit})


def _paise(value):
    """Decimal rupees -> whole paise, so the browser never does money maths with decimals."""
    return int((Decimal(value) * 100).to_integral_value())


def _product_json(p, exact=False):
    return {
        "id": p.pk,
        "name": p.name,
        "code": p.code or "",
        "barcode": p.barcode or "",
        "price": _paise(p.sale_price),
        "mrp": _paise(p.mrp or p.sale_price),
        "gst": float(p.gst_rate),
        "track": p.track_stock,
        "stock": float(p.stock) if p.track_stock else None,
        "unit": p.unit,
        "exact": exact,
    }


@login_required
def product_search(request):
    q = request.GET.get("q", "").strip()
    if not q:
        return JsonResponse({"results": []})

    base = with_stock(Product.objects.filter(is_active=True))

    # An exact barcode or code match always comes first (this is what a scanner produces)
    exact = list(base.filter(Q(barcode__iexact=q) | Q(code__iexact=q))[:5])
    exact_ids = [p.pk for p in exact]

    others = (
        base.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(barcode__icontains=q))
        .exclude(pk__in=exact_ids)
        .order_by("name")[:20]
    )

    results = [_product_json(p, exact=True) for p in exact] + [_product_json(p) for p in others]
    return JsonResponse({"results": results})


@login_required
def customer_search(request):
    """Name and phone only. Cashiers can pick a customer without seeing GST or address details."""
    q = request.GET.get("q", "").strip()
    qs = Contact.objects.filter(is_customer=True, is_active=True, is_system=False)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
    results = [{"id": c.pk, "name": c.name, "phone": c.phone} for c in qs.order_by("name")[:20]]
    return JsonResponse({"results": results})


@login_required
@require_POST
def checkout(request):
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid request."}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Invalid request."}, status=400)

    try:
        result = create_sale(request.user, data)
    except CheckoutError as exc:
        return JsonResponse({"error": str(exc)}, status=exc.status)
    except Exception:
        logger.exception("Checkout failed")
        return JsonResponse({"error": "Something went wrong. The bill was NOT saved. Please try again."}, status=500)
    return JsonResponse(result)