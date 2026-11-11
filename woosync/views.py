from functools import wraps

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from inventory.models import Product
from inventory.stock import qty_fmt, with_stock

from .client import WooClient, WooError
from .models import CLOSED, HOLDING, SyncRun, SyncState, WebOrder, WooLink
from .sync import (
    MASS_ZERO_LIMIT, STALE_LOCK, SyncBusy, SyncOff, match_products, planned_pushes,
    reconcile_stock, run_sync,
)


def admin_only(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.is_admin_role:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def staff_only(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return view(request, *args, **kwargs)
    return wrapper


# =====================  Admin: the sync itself  =====================
@admin_only
def home(request):
    state = SyncState.load()
    latest_run = SyncRun.objects.first()
    running = bool(state.running_since and timezone.now() - state.running_since < STALE_LOCK)
    configured, problem = True, ""
    try:
        WooClient()
    except WooError as exc:
        configured, problem = False, str(exc)

    online = WooLink.objects.filter(sell_online=True)
    return render(request, "woosync/home.html", {
        "state": state, "configured": configured, "problem": problem,
        "running": running, "latest_run": latest_run,
        "imported_orders": WebOrder.objects.count(),
        "linked": WooLink.objects.filter(woo_product_id__isnull=False).count(),
        "online": online.count(),
        "online_ready": online.filter(woo_product_id__isnull=False, woo_type="simple").count(),
        "runs": SyncRun.objects.all()[:8],
        "new_orders": WebOrder.objects.filter(handled=False, status__in=HOLDING).count(),
        "unmatched_orders": WebOrder.objects.filter(
            status__in=HOLDING, items__product__isnull=True, items__quantity__gt=0
        ).distinct().count(),
    })


@admin_only
@require_POST
def test_connection(request):
    try:
        client = WooClient()
        _, products = client.get_page("products", {"per_page": 1, "_fields": "id"})
        _, orders = client.get_page("orders", {"per_page": 1, "_fields": "id"})
    except WooError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Connected. The website has {products.get('X-WP-Total', '?')} products and {orders.get('X-WP-Total', '?')} "
            "orders. Reading works. Writing is checked the first time stock is sent.",
        )
    return redirect("woosync:home")


@admin_only
@require_POST
def toggle(request):
    state = SyncState.load()
    if state.is_enabled:
        state.is_enabled = False
        state.save(update_fields=["is_enabled"])
        messages.success(request, "Website sync is OFF. Nothing is sent or imported until you switch it on.")
        return redirect("woosync:home")

    try:
        WooClient()
    except WooError as exc:
        messages.error(request, str(exc))
        return redirect("woosync:home")

    state.is_enabled = True
    if state.orders_since is None:                          # the first time: only orders from now on are brought in
        state.orders_since = timezone.now()
    state.save(update_fields=["is_enabled", "orders_since"])
    messages.success(request, "Website sync is ON.")
    return redirect("woosync:home")


@admin_only
@require_POST
def save_settings(request):
    raw = request.POST.get("holdback", "").strip()
    if raw.isdigit() and int(raw) <= 1000:
        state = SyncState.load()
        state.holdback_units = int(raw)
        state.save(update_fields=["holdback_units"])
        messages.success(request, f"The website will be shown {raw} unit(s) less than the real stock.")
    else:
        messages.error(request, "Enter a whole number from 0 to 1000.")
    return redirect("woosync:home")


@admin_only
@require_POST
def match_view(request):
    try:
        result = match_products(WooClient())
    except WooError as exc:
        messages.error(request, str(exc))
        return redirect("woosync:home")

    messages.success(
        request,
        f"Matched {result['matched']} products ({result['unsupported']} are not simple products, so they cannot be "
        f"synced). {len(result['no_sku'])} website products have no SKU, and {len(result['unknown_sku'])} have a SKU "
        "that no POS product uses.",
    )
    if result["unknown_sku"]:
        messages.warning(request, "SKU not found in the POS: " + "; ".join(result["unknown_sku"][:5]))
    return redirect("woosync:products")


@admin_only
@require_POST
def sync_now(request):
    try:
        run = run_sync("manual", allow_mass_zero=request.POST.get("override") == "1")
    except SyncOff as exc:
        messages.error(request, str(exc))
    except SyncBusy as exc:
        messages.warning(request, str(exc))
    else:
        (messages.success if run.ok else messages.error)(request, run.summary[:400])
    return redirect("woosync:home")


@admin_only
def products(request):
    state = SyncState.load()

    if request.method == "POST":
        action = request.POST.get("action")
        ids = [int(x) for x in request.POST.getlist("sel") if x.isdigit()]
        if action == "resend":
            WooLink.objects.update(last_pushed_qty=None)
            messages.success(request, "All stock will be sent again on the next sync.")
        elif action in ("enable", "disable") and ids:
            if action == "enable":
                count = 0
                for p in Product.objects.filter(pk__in=ids, track_stock=True):
                    link, _ = WooLink.objects.get_or_create(product=p)
                    if not link.sell_online:
                        link.sell_online = True
                        link.save(update_fields=["sell_online"])
                    count += 1
                messages.success(request, f"{count} product(s) will be sold online. Check the Preview before switching the sync on.")
            else:
                WooLink.objects.filter(product_id__in=ids).update(sell_online=False)
                messages.success(request, "Those products are no longer sent to the website. Their website stock stays as it is.")
        else:
            messages.warning(request, "Tick at least one product first.")
        return redirect(request.get_full_path())

    q = request.GET.get("q", "").strip()
    show = request.GET.get("show", "all")
    qs = with_stock(Product.objects.select_related("woo_link"))
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q))
    if show == "online":
        qs = qs.filter(woo_link__sell_online=True)
    elif show == "unlinked":
        qs = qs.filter(Q(woo_link__isnull=True) | Q(woo_link__woo_product_id__isnull=True))
    else:
        show = "all"

    paginator = Paginator(qs.order_by("name"), 50)
    page = paginator.get_page(request.GET.get("page"))
    plan = {link.product_id: target for link, _, target in planned_pushes(state.holdback_units)}

    rows = []
    for p in page:
        link = p.woo_link if hasattr(p, "woo_link") else None
        if link is None or link.woo_product_id is None:
            site = "Not matched. Press Match products."
        elif link.woo_type != "simple":
            site = f"Website type: {link.woo_type or 'unknown'} (not synced)"
        else:
            site = f"Website #{link.woo_product_id}"
        rows.append({
            "p": p, "link": link, "site": site,
            "stock": qty_fmt(p.stock) if p.track_stock else "\u2014",
            "sent": "" if link is None or link.last_pushed_qty is None else link.last_pushed_qty,
            "next": plan.get(p.pk, ""),
        })

    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "woosync/products.html", {
        "rows": rows, "page": page, "total": paginator.count, "q": q, "show": show,
        "querystring": params.urlencode(),
    })


@admin_only
def preview(request):
    state = SyncState.load()
    plan = planned_pushes(state.holdback_units)
    rows = [{
        "name": p.name, "code": p.code or "", "stock": qty_fmt(p.stock),
        "old": link.last_pushed_qty, "new": target,
        "drop": target == 0 and link.last_pushed_qty != 0,
    } for link, p, target in plan]
    drops = sum(1 for r in rows if r["drop"])

    skipped = []
    for link in WooLink.objects.filter(sell_online=True).select_related("product"):
        if link.woo_product_id is None:
            skipped.append((link.product.name, "not matched to a website product yet"))
        elif link.woo_type != "simple":
            skipped.append((link.product.name, f"the website product is '{link.woo_type or 'unknown'}', not simple"))
        elif not link.product.track_stock:
            skipped.append((link.product.name, "stock is not tracked in the POS"))

    return render(request, "woosync/preview.html", {
        "rows": rows, "skipped": skipped, "drops": drops, "limit": MASS_ZERO_LIMIT,
        "guard": drops > MASS_ZERO_LIMIT, "holdback": state.holdback_units, "state": state,
    })


@admin_only
def log_page(request):
    paginator = Paginator(SyncRun.objects.all(), 50)
    return render(request, "woosync/log.html", {"page": paginator.get_page(request.GET.get("page"))})


# =====================  Staff: website orders  =====================
@staff_only
def orders(request):
    q = request.GET.get("q", "").strip()
    show = request.GET.get("show", "new")
    if show not in ("new", "active", "closed", "all"):
        show = "new"

    base = WebOrder.objects.all()
    if q:
        base = base.filter(
            Q(number__icontains=q) | Q(customer_name__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q)
        )
    tabs = [
        ("new", "To handle", base.filter(status__in=HOLDING, handled=False).count()),
        ("active", "All active", base.filter(status__in=HOLDING).count()),
        ("closed", "Cancelled / refunded", base.filter(status__in=CLOSED).count()),
        ("all", "Everything", base.count()),
    ]
    if show == "new":
        base = base.filter(status__in=HOLDING, handled=False)
    elif show == "active":
        base = base.filter(status__in=HOLDING)
    elif show == "closed":
        base = base.filter(status__in=CLOSED)

    qs = base.annotate(
        unmatched=Count("items", filter=Q(items__product__isnull=True, items__quantity__gt=0), distinct=True),
        n_items=Count("items", filter=Q(items__quantity__gt=0), distinct=True),
    )
    paginator = Paginator(qs.order_by("-woo_created", "-id"), 25)
    page = paginator.get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "woosync/orders.html", {
        "page": page, "q": q, "show": show, "tabs": tabs, "querystring": params.urlencode(),
    })


@staff_only
def order_detail(request, pk):
    order = get_object_or_404(WebOrder, pk=pk)
    items = list(order.items.select_related("product"))
    return render(request, "woosync/order_detail.html", {
        "order": order, "items": items,
        "unmatched": any(i.product_id is None and i.quantity > 0 for i in items),
    })


@staff_only
@require_POST
def order_handled(request, pk):
    order = get_object_or_404(WebOrder, pk=pk)
    order.handled = not order.handled
    order.handled_by = request.user if order.handled else None
    order.handled_at = timezone.now() if order.handled else None
    order.save(update_fields=["handled", "handled_by", "handled_at", "updated_at"])
    messages.success(request, f"Order #{order.number} marked as {'handled' if order.handled else 'not handled'}.")
    return redirect("woosync:order_detail", pk=pk)


@admin_only
@require_POST
def order_recheck(request, pk):
    with transaction.atomic():
        order = get_object_or_404(WebOrder.objects.select_for_update(), pk=pk)
        moved = reconcile_stock(order)
    messages.success(request, f"Stock re-checked. {moved} stock change(s) made.")
    return redirect("woosync:order_detail", pk=pk)