import json
from .gst import split_tax
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
from .models import CashMovement, DrawerEvent, HeldSale, Payment, Sale, SaleItem, SaleReturn, SaleReturnItem
from inventory.models import Product, Unit
from inventory.stock import with_stock
from people.models import Contact
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from .models import Payment, Sale, SaleItem, SaleReturn, SaleReturnItem
from .services import CheckoutError, create_return, create_sale, to_paise, void_sale
from promotions.engine import rules_for
from storeconfig import printing
from storeconfig.models import PrintSettings, StoreSettings
logger = logging.getLogger(__name__)


def _store():
    return StoreSettings.load().as_store_info()


@login_required
def screen(request):
    cfg = StoreSettings.load()
    # Admins have no discount limit, so the page shows the limit to Cashiers only
    limit = "" if request.user.is_admin_role else f"{cfg.cashier_max_discount_percent.normalize():f}"
    print_dialog = None
    if request.user.is_admin_role:
        print_dialog = {"settings": PrintSettings.load().as_dict(), "defaults": printing.LABEL_DEFAULTS}
    return render(request, "pos/screen.html", {
        "max_discount": limit, "store": cfg.as_store_info(),
        "locked": bool(request.session.get("pos_locked")), "print_dialog": print_dialog,
    })


@login_required
def screen(request):
    # Admins have no discount limit, so the page shows the limit to Cashiers only
    limit = "" if request.user.is_admin_role else str(getattr(settings, "POS_CASHIER_MAX_DISCOUNT_PERCENT", 10))
    print_dialog = None
    if request.user.is_admin_role:
        print_dialog = {"settings": PrintSettings.load().as_dict(), "defaults": printing.LABEL_DEFAULTS}
    return render(request, "pos/screen.html", {
        "max_discount": limit, "store": _store(), "print_dialog": print_dialog,
    })


def _paise(value):
    """Decimal rupees -> whole paise, so the browser never does money maths with decimals."""
    return int((Decimal(value) * 100).to_integral_value())


def _product_json(p, rules, exact=False):
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
        "promos": rules,
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

    others = list(
        base.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(barcode__icontains=q))
        .exclude(pk__in=exact_ids)
        .order_by("name")[:20]
    )

    rules = rules_for([p.pk for p in exact + others])
    results = [_product_json(p, rules.get(p.pk, []), exact=True) for p in exact] + [
        _product_json(p, rules.get(p.pk, [])) for p in others
    ]
    return JsonResponse({"results": results})


@login_required
def promotion_rules(request):
    """The promotions running right now for the products in the cart. The POS re-checks them before payment."""
    ids = [int(x) for x in request.GET.get("ids", "").split(",") if x.strip().isdigit()][:200]
    return JsonResponse({"rules": {str(pid): r for pid, r in rules_for(ids).items()}})


@login_required
def customer_search(request):
    """Name and phone only. Cashiers can pick a customer without seeing GST or address details."""
    q = request.GET.get("q", "").strip()
    qs = Contact.objects.filter(is_customer=True, is_supplier=False, is_active=True, is_system=False)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
    results = [{"id": c.pk, "name": c.name, "phone": c.phone} for c in qs.order_by("name")[:20]]
    return JsonResponse({"results": results})


@login_required
@require_POST
def checkout(request):
    if request.session.get("pos_locked"):
        return JsonResponse({"error": "The POS is locked. Unlock it first."}, status=423)
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
def _print_context(sale, items):
    ps = PrintSettings.load().as_context()
    order_no = None
    if ps["r_order_no"]:
        day = timezone.localtime(sale.created_at).date()
        order_no = Sale.objects.filter(created_at__date=day, id__lte=sale.pk).count()
    return {
        "ps": ps, "L": ps["labels"],
        "short_no": sale.invoice_no.rsplit("-", 1)[-1],
        "order_no": order_no,
        "items_count": len(items),
        "qty_total": sum((i["qty"] for i in items), Decimal("0")),
    }

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
        cgst, sgst = split_tax(g["tax"])
        tax_summary.append({**g, "half_rate": rate / 2, "cgst": cgst, "sgst": sgst})

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
        **_print_context(sale, items),
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
    whole_units = Unit.whole_quantity_codes()
    rows = []
    for it in sale.items.all():
        back_q = done[it.pk]["q"] if it.pk in done else Decimal("0")
        back_a = done[it.pk]["a"] if it.pk in done else Decimal("0")
        rows.append({
            "it": it, "returned": back_q,
            "left_q": it.quantity - back_q, "left_a": it.line_total - back_a,
            "whole": it.unit in whole_units, "value": posted.get(f"qty_{it.pk}", ""),
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
    
# =====================  Saved sales, drawer, refund lookup, lock  =====================
def _bad(message="Invalid request.", status=400):
    return JsonResponse({"error": message}, status=status)


def _body(request):
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _int(value, lo=0, hi=10**9):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError
    return value


@login_required
def held_list(request):
    rows = HeldSale.objects.select_related("customer", "user")[:50]
    return JsonResponse({"results": [
        {
            "id": h.pk,
            "customer": h.customer.name if h.customer else "Walk-in customer",
            "items": h.item_count,
            "total": str(h.total),
            "by": h.user.get_username(),
            "at": timezone.localtime(h.created_at).strftime("%d %b, %I:%M %p"),
        }
        for h in rows
    ]})


@login_required
@require_POST
def held_save(request):
    if request.session.get("pos_locked"):
        return _bad("The POS is locked.", 423)
    data = _body(request)
    if data is None:
        return _bad()
    raw = data.get("items")
    if not isinstance(raw, list) or not raw or len(raw) > 200:
        return _bad("Add items to the bill first.")

    try:
        items = []
        for r in raw:
            if not isinstance(r, dict):
                raise ValueError
            qty = Decimal(str(r.get("qty")))
            if not qty.is_finite() or qty <= 0 or qty > 100000:
                raise ValueError
            kind = r.get("dKind")
            if kind not in (None, "percent", "amount"):
                raise ValueError
            price = r.get("price")
            items.append({
                "id": _int(r.get("id"), 1),
                "qty": format(qty.normalize(), "f"),
                "dKind": kind,
                "dValue": _int(r.get("dValue") or 0),
                "price": None if price is None else _int(price, 1),
            })
        bill = data.get("bill") or {}
        bill_kind = bill.get("kind")
        if bill_kind not in (None, "percent", "amount"):
            raise ValueError
        bill_value = _int(bill.get("value") or 0)
        total = _int(data.get("total") or 0)
    except (ValueError, InvalidOperation, TypeError):
        return _bad()

    names = dict(Product.objects.filter(pk__in=[i["id"] for i in items]).values_list("pk", "name"))
    if len(names) != len({i["id"] for i in items}):
        return _bad("A product on the bill no longer exists.")
    for i in items:
        i["name"] = names[i["id"]]

    customer = None
    cid = data.get("customer_id")
    if cid not in (None, ""):
        customer = Contact.objects.filter(pk=cid, is_customer=True, is_supplier=False, is_active=True).first()

    hold = HeldSale.objects.create(
        user=request.user, customer=customer, item_count=len(items),
        total=(Decimal(total) / 100).quantize(Decimal("0.01")),
        data={"items": items, "bill": {"kind": bill_kind, "value": bill_value}},
    )
    return JsonResponse({"ok": True, "label": f"#{hold.pk}"})


@login_required
@require_POST
def held_resume(request, pk):
    """Hands the saved bill back with today's prices and stock, and removes it from the saved list."""
    if request.session.get("pos_locked"):
        return _bad("The POS is locked.", 423)
    with transaction.atomic():
        hold = HeldSale.objects.select_related("customer").filter(pk=pk).first()
        if hold is None or not HeldSale.objects.filter(pk=pk).delete()[0]:
            return _bad("That saved sale was already opened or deleted.", 404)

        saved = hold.data.get("items", [])
        products = {
            p.pk: p for p in with_stock(Product.objects.filter(pk__in=[i["id"] for i in saved], is_active=True))
        }
        product_rules = rules_for(list(products))
        items, skipped = [], []
        for i in saved:
            p = products.get(i["id"])
            if p is None:
                skipped.append(i.get("name") or f"product {i['id']}")
                continue
            items.append({
                "product": _product_json(p, product_rules.get(p.pk, [])), "qty": i["qty"],
                "dKind": i.get("dKind"), "dValue": i.get("dValue", 0), "price": i.get("price"),
            })

        customer = None
        c = hold.customer
        if c and c.is_customer and c.is_active:
            customer = {"id": c.pk, "name": c.name}

        return JsonResponse({
            "ok": True, "items": items, "skipped": skipped,
            "bill": hold.data.get("bill") or {"kind": None, "value": 0}, "customer": customer,
        })


@login_required
@require_POST
def held_delete(request, pk):
    HeldSale.objects.filter(pk=pk).delete()
    return JsonResponse({"ok": True})


@login_required
@require_POST
def drawer_open(request):
    if request.session.get("pos_locked"):
        return _bad("The POS is locked.", 423)
    data = _body(request) or {}
    reason = str(data.get("reason") or "").strip()[:120]
    if not reason:
        return _bad("Choose a reason.")
    DrawerEvent.objects.create(user=request.user, reason=reason)
    return JsonResponse({"ok": True})


@login_required
@require_POST
def cash_move(request):
    if request.session.get("pos_locked"):
        return _bad("The POS is locked.", 423)
    data = _body(request)
    if data is None:
        return _bad("Invalid request.")

    direction = data.get("direction")
    if direction not in CashMovement.Direction.values:
        return _bad("Choose cash in or cash out.")
    reason = str(data.get("reason") or "").strip()
    if not reason or len(reason) > 60:
        return _bad("Choose a valid reason.")
    note = str(data.get("note") or "").strip()
    if len(note) > 60:
        return _bad("The note must be 60 characters or fewer.")
    try:
        amount = to_paise(data.get("amount"), "Amount")
    except CheckoutError as exc:
        return _bad(str(exc))
    if amount <= 0:
        return _bad("Amount must be greater than zero.")

    movement = CashMovement.objects.create(
        user=request.user,
        direction=direction,
        amount=Decimal(amount) / 100,
        reason=reason,
        note=note,
    )
    return JsonResponse({
        "ok": True,
        "movement_id": movement.pk,
        "direction": movement.direction,
        "amount": f"{movement.amount:.2f}",
    })


@login_required
def find_sale(request):
    """Finds a bill by invoice number so the Refund button can open its return page."""
    if not request.user.is_admin_role:
        return _bad("Only an admin can process refunds.", 403)
    q = request.GET.get("q", "").strip()
    sale = Sale.objects.filter(invoice_no__iexact=q).first() if q else None
    if sale is None and q.isdigit():
        sale = Sale.objects.filter(invoice_no=f"AF-{timezone.localdate().year}-{int(q):04d}").first()
    if sale is None:
        return _bad("No bill found with that number.", 404)
    return JsonResponse({"ok": True, "invoice_no": sale.invoice_no,
                         "url": reverse("pos:sale_return", args=[sale.pk])})


@login_required
@require_POST
def lock(request):
    request.session["pos_locked"] = True
    return JsonResponse({"ok": True})


@login_required
@require_POST
def unlock(request):
    tries = request.session.get("pos_unlock_tries", 0)
    if tries >= 5:
        return _bad("Too many wrong attempts. Please log out and sign in again.", 429)
    data = _body(request) or {}
    if request.user.check_password(str(data.get("password") or "")):
        request.session["pos_locked"] = False
        request.session["pos_unlock_tries"] = 0
        return JsonResponse({"ok": True})
    request.session["pos_unlock_tries"] = tries + 1
    return _bad("Wrong password.", 403)


@login_required
@require_POST
def print_settings_save(request):
    if not request.user.is_admin_role:
        return _bad("Only an admin can change print settings.", 403)
    data = _body(request)
    if data is None:
        return _bad()
    try:
        clean = printing.clean_payload(data)
    except ValueError as exc:
        return _bad(str(exc))
    ps = PrintSettings.load()
    for name, value in clean.items():
        setattr(ps, name, value)
    ps.updated_by = request.user
    ps.save()
    return JsonResponse({"ok": True, "settings": ps.as_dict()})