import json
import logging
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from storeconfig.models import StoreSettings
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inventory.models import Product
from inventory.stock import with_stock
from people.models import Contact

from .models import Payment, Sale, SaleItem, SaleReturn, SaleReturnItem
from .services import COUNT_UNITS, CheckoutError, create_return, create_sale, void_sale

logger = logging.getLogger(__name__)


def _store():
    return StoreSettings.load().as_store_info()


@login_required
def screen(request):
    cfg = StoreSettings.load()
    # Admins have no discount limit, so the page shows the limit to Cashiers only
    limit = "" if request.user.is_admin_role else f"{cfg.cashier_max_discount_percent.normalize():f}"
    return render(request, "pos/screen.html", {"max_discount": limit, "store": cfg.as_store_info()})


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
                "store": sale.store_info or _store(),     # old bills use the current details
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


# =====================  Sales list, bill detail, void, return  =====================
def _parse_date(text):
    try:
        return date.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def _returned_totals(sale):
    """{sale_item_id: {"q": quantity returned so far, "a": amount refunded so far}}"""
    rows = (
        SaleReturnItem.objects.filter(sale_item__sale=sale)
        .values("sale_item_id")
        .annotate(q=Sum("quantity"), a=Sum("refund_amount"))
    )
    return {r["sale_item_id"]: r for r in rows}


@login_required
def sale_list(request):
    admin = request.user.is_admin_role
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    method = request.GET.get("method", "")
    cashier_id = request.GET.get("cashier", "") if admin else ""
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")

    base = Sale.objects.all()
    if not admin:
        base = base.filter(cashier=request.user)          # a cashier sees only their own bills
    if q:
        base = base.filter(
            Q(invoice_no__icontains=q) | Q(customer__name__icontains=q) | Q(customer__phone__icontains=q)
        )
    if status in Sale.Status.values:
        base = base.filter(status=status)
    if method in Payment.Method.values:
        base = base.filter(Exists(Payment.objects.filter(sale=OuterRef("pk"), method=method)))
    if cashier_id.isdigit():
        base = base.filter(cashier_id=cashier_id)
    if _parse_date(date_from):
        base = base.filter(created_at__date__gte=_parse_date(date_from))
    if _parse_date(date_to):
        base = base.filter(created_at__date__lte=_parse_date(date_to))

    totals = base.filter(status=Sale.Status.COMPLETED).aggregate(n=Count("id"), s=Sum("total"))

    qs = (
        base.select_related("customer", "cashier")
        .prefetch_related("payments")
        .annotate(return_count=Count("returns", distinct=True))
    )
    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get("page"))

    rows = [(s, ", ".join(sorted({p.get_method_display() for p in s.payments.all()}))) for s in page]

    params = request.GET.copy()
    params.pop("page", None)

    return render(request, "pos/sale_list.html", {
        "rows": rows, "page": page, "total": paginator.count, "totals": totals,
        "q": q, "status": status, "method": method, "cashier_id": cashier_id,
        "date_from": date_from, "date_to": date_to,
        "statuses": Sale.Status.choices, "methods": Payment.Method.choices,
        "cashiers": get_user_model().objects.order_by("username") if admin else [],
        "querystring": params.urlencode(), "can_manage": admin,
    })


@login_required
def sale_detail(request, pk):
    sale = _sale_for(request, pk)
    admin = request.user.is_admin_role
    done = _returned_totals(sale)

    rows, any_left = [], False
    for it in sale.items.all():
        back = done[it.pk]["q"] if it.pk in done else Decimal("0")
        left = it.quantity - back
        any_left = any_left or left > 0
        rows.append({"it": it, "returned": back, "left": left})

    returns = list(sale.returns.select_related("processed_by").prefetch_related("items"))
    completed = sale.status == Sale.Status.COMPLETED
    bill = _bill_context(request, sale)

    return render(request, "pos/sale_detail.html", {
        "sale": sale, "rows": rows, "returns": returns,
        "pay_lines": bill["pay_lines"],
        "refunded": sum((r.refund_total for r in returns), Decimal("0")),
        "can_return": admin and completed and any_left,
        "can_void": admin and completed and not returns,
        "is_admin": admin,
    })


@login_required
@require_POST
def sale_void(request, pk):
    if not request.user.is_admin_role:
        raise PermissionDenied
    try:
        sale = void_sale(request.user, pk, request.POST.get("reason", ""))
    except CheckoutError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{sale.invoice_no} was voided and its stock was put back.")
    return redirect("pos:sale_detail", pk=pk)


@login_required
def sale_return(request, pk):
    if not request.user.is_admin_role:
        raise PermissionDenied
    sale = get_object_or_404(Sale.objects.select_related("customer"), pk=pk)
    posted = request.POST if request.method == "POST" else {}

    if request.method == "POST":
        quantities = {}
        for key, value in request.POST.items():
            if key.startswith("qty_") and key[4:].isdigit():
                quantities[int(key[4:])] = value
        data = {
            "quantities": quantities,
            "refund_method": request.POST.get("refund_method"),
            "reason": request.POST.get("reason", ""),
            "client_token": request.POST.get("client_token", ""),
        }
        try:
            ret, duplicate = create_return(request.user, sale.pk, data)
        except CheckoutError as exc:
            messages.error(request, str(exc))
        else:
            if duplicate:
                messages.info(request, f"{ret.return_no} was already recorded.")
            else:
                messages.success(request, f"Return {ret.return_no} saved. Refund \u20b9{ret.refund_total:,.2f} ({ret.get_refund_method_display()}).")
            return redirect("pos:sale_detail", pk=sale.pk)

    done = _returned_totals(sale)
    rows = []
    for it in sale.items.all():
        back_q = done[it.pk]["q"] if it.pk in done else Decimal("0")
        back_a = done[it.pk]["a"] if it.pk in done else Decimal("0")
        rows.append({
            "it": it, "returned": back_q,
            "left_q": it.quantity - back_q, "left_a": it.line_total - back_a,
            "whole": it.unit in COUNT_UNITS, "value": posted.get(f"qty_{it.pk}", ""),
        })

    return render(request, "pos/sale_return.html", {
        "sale": sale, "rows": rows, "methods": Payment.Method.choices,
        "token": posted.get("client_token") or uuid.uuid4().hex,
        "reason": posted.get("reason", ""), "method": posted.get("refund_method", "cash"),
        "voided": sale.status == Sale.Status.VOIDED,
    })


@login_required
def return_list(request):
    if not request.user.is_admin_role:
        raise PermissionDenied
    qs = SaleReturn.objects.select_related("sale", "sale__customer", "processed_by")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(return_no__icontains=q) | Q(sale__invoice_no__icontains=q) | Q(sale__customer__name__icontains=q))

    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "pos/return_list.html", {
        "page": page, "total": paginator.count, "q": q, "querystring": params.urlencode(),
    })