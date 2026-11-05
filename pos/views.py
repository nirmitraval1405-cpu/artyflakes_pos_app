import json
import logging
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from inventory.models import Product
from inventory.stock import with_stock
from people.models import Contact

from .models import Payment, Sale
from .services import CheckoutError, create_sale

logger = logging.getLogger(__name__)


def _store():
    info = {"name": "Artyflakes", "tagline": "", "address": "", "phone": "", "gstin": "", "footer": ""}
    info.update(getattr(settings, "STORE", {}))
    return info


@login_required
def screen(request):
    # Admins have no discount limit, so the page shows the limit to Cashiers only
    limit = "" if request.user.is_admin_role else str(getattr(settings, "POS_CASHIER_MAX_DISCOUNT_PERCENT", 10))
    return render(request, "pos/screen.html", {"max_discount": limit, "store": _store()})


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


# =====================  Receipt, invoice, note  =====================
def _sale_for(request, pk):
    """Admins can open any bill. A cashier can open only the bills they made."""
    sale = get_object_or_404(Sale.objects.select_related("customer", "cashier"), pk=pk)
    if not request.user.is_admin_role and sale.cashier_id != request.user.pk:
        raise Http404
    return sale


def _bill_context(request, sale):
    items, groups = [], {}
    for it in sale.items.all():
        taxable = it.line_total - it.tax_amount
        items.append({
            "name": it.product_name, "hsn": it.hsn_code, "qty": it.quantity, "unit": it.unit,
            "price": it.unit_price, "discount": it.discount, "gst_rate": it.gst_rate,
            "taxable": taxable, "tax": it.tax_amount, "total": it.line_total,
        })
        g = groups.setdefault(it.gst_rate, {"rate": it.gst_rate, "taxable": Decimal("0"), "tax": Decimal("0")})
        g["taxable"] += taxable
        g["tax"] += it.tax_amount

    # GST is shown split half and half (CGST + SGST), assuming the customer is in the same state
    tax_summary = []
    for rate in sorted(groups):
        g = groups[rate]
        if g["rate"] <= 0:
            continue
        cgst = (g["tax"] / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        tax_summary.append({**g, "half_rate": rate / 2, "cgst": cgst, "sgst": g["tax"] - cgst})

    # Cash is stored after change was handed back; show what the customer actually handed over
    pay_lines, has_cash = [], False
    for p in sale.payments.all():
        amount = p.amount
        if p.method == Payment.Method.CASH:
            has_cash = True
            amount += sale.change_due
        pay_lines.append({"label": p.get_method_display(), "amount": amount, "reference": p.reference})
    if sale.change_due > 0 and not has_cash:
        pay_lines.append({"label": "Cash", "amount": sale.change_due, "reference": ""})

    return {
        "sale": sale,
        "items": items,
        "pay_lines": pay_lines,
        "tax_summary": tax_summary,
        "taxable_total": sale.total - sale.tax_total,
        "cgst_total": sum((g["cgst"] for g in tax_summary), Decimal("0")),
        "sgst_total": sum((g["sgst"] for g in tax_summary), Decimal("0")),
        "walk_in": sale.customer.is_system,
        "cashier_name": sale.cashier.get_full_name() or sale.cashier.username,
        "store": _store(),
        "autoprint": request.GET.get("print") == "1",
    }


@login_required
def receipt(request, pk):
    return render(request, "pos/receipt.html", _bill_context(request, _sale_for(request, pk)))


@login_required
def invoice(request, pk):
    return render(request, "pos/invoice.html", _bill_context(request, _sale_for(request, pk)))


@login_required
@require_POST
def sale_note(request, pk):
    sale = _sale_for(request, pk)
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid request."}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Invalid request."}, status=400)
    sale.note = str(data.get("note", "")).strip()[:255]
    sale.save(update_fields=["note", "updated_at"])
    return JsonResponse({"ok": True})