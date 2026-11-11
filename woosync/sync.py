import logging
import math
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from inventory.models import Product, StockMovement
from inventory.stock import with_stock

from .client import WooClient, WooError
from .models import HOLDING, SyncRun, SyncState, WebOrder, WebOrderItem, WooLink

log = logging.getLogger("woosync")

IGNORED = {"checkout-draft", "auto-draft"}
BATCH = 100                         # products per request to the website
MASS_ZERO_LIMIT = 10                # a scheduled run will not send more than this many "set to zero" updates
STALE_LOCK = timedelta(minutes=10)  # a run that died leaves its lock behind; it is ignored after this long
OVERLAP = timedelta(minutes=5)      # orders are re-read from a little before the bookmark, so none is missed
ERROR_CAP = 15


class SyncBusy(Exception):
    pass


class SyncOff(Exception):
    pass


class InvalidOrderPayload(Exception):
    pass


# ---------- Small helpers ----------
def _dt(text):
    if not text:
        return None
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)


def _money(value):
    try:
        d = Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0.00")
    if not d.is_finite() or abs(d) >= Decimal("1000000000000"):
        return Decimal("0.00")
    return d.quantize(Decimal("0.01"))


def _qty(value):
    try:
        d = Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0.000")
    if not d.is_finite() or d < 0 or d >= Decimal("1000000000"):
        return Decimal("0.000")
    return d.quantize(Decimal("0.001"))


def _address(a):
    lines = [
        " ".join(x for x in (a.get("first_name"), a.get("last_name")) if x),
        a.get("company"), a.get("address_1"), a.get("address_2"),
        " ".join(x for x in (a.get("city"), a.get("state"), a.get("postcode")) if x),
        a.get("country"),
    ]
    return "\n".join(str(x).strip() for x in lines if x and str(x).strip())[:500]


def _financial_snapshot(payload):
    """Keep the WooCommerce accounting values needed for a later exact sale conversion."""
    snapshot = {
        key: payload.get(key)
        for key in (
            "currency", "prices_include_tax", "discount_total", "discount_tax",
            "shipping_total", "shipping_tax", "cart_tax", "total_tax", "total",
        )
    }
    row_fields = {
        "line_items": (
            "id", "product_id", "variation_id", "name", "sku", "quantity",
            "subtotal", "subtotal_tax", "total", "total_tax", "taxes",
        ),
        "tax_lines": ("id", "rate_code", "rate_id", "label", "compound", "tax_total", "shipping_tax_total"),
        "shipping_lines": ("id", "method_title", "method_id", "instance_id", "total", "total_tax", "taxes"),
        "coupon_lines": ("id", "code", "discount", "discount_tax"),
        "refunds": ("id", "reason", "total", "date_created"),
    }
    for key, fields in row_fields.items():
        rows = payload.get(key) or []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise InvalidOrderPayload(f"{key} must be a list of objects")
        snapshot[key] = [
            {field: row[field] for field in fields if field in row}
            for row in rows
        ]
    return snapshot


def _match(woo_ids, sku):
    """Finds the POS product for a website product: by its linked website id first, then by SKU = POS code."""
    for wid in woo_ids:
        if wid:
            link = WooLink.objects.select_related("product").filter(woo_product_id=wid).first()
            if link:
                return link.product
    sku = (sku or "").strip()
    if sku:
        return Product.objects.filter(code__iexact=sku).first()
    return None


# ---------- Orders: website -> POS ----------
def reconcile_stock(order):
    """Makes the stock ledger agree with the order's status and items. Safe to run again and again."""
    holds = order.status in HOLDING
    moves = []
    for item in order.items.select_related("product"):
        if item.product_id is None:
            product = _match([item.woo_product_id], item.sku)
            if product is not None:
                item.product = product
                item.save(update_fields=["product"])

        want = item.quantity if holds else Decimal("0.000")
        delta = want - item.applied_qty                  # positive: take more out of stock
        if delta == 0 or item.product_id is None:
            continue                                      # an unmatched line stays flagged until its SKU is fixed

        if item.product.track_stock:
            moves.append(StockMovement(
                product_id=item.product_id, quantity=-delta,
                movement_type=StockMovement.Type.WEB_ORDER if delta > 0 else StockMovement.Type.WEB_ORDER_RETURN,
                source=StockMovement.Source.WOOCOMMERCE,
                reference=f"WC-{order.number}"[:50], note=f"Website order #{order.number} ({order.status})"[:255],
            ))
        item.applied_qty = want
        item.save(update_fields=["applied_qty"])

    if moves:
        StockMovement.objects.bulk_create(moves)
    return len(moves)


def apply_order(payload):
    """Copies one website order into the POS and keeps stock in step with its status. Returns "new", "updated" or None."""
    if not isinstance(payload, dict):
        return None
    woo_id = payload.get("id")
    if not isinstance(woo_id, int) or isinstance(woo_id, bool):
        return None
    status = str(payload.get("status") or "")
    if status in IGNORED:
        return None

    line_items = payload.get("line_items") or []
    if not isinstance(line_items, list) or any(not isinstance(line, dict) for line in line_items):
        raise InvalidOrderPayload("line_items must be a list of objects")
    financial_data = _financial_snapshot(payload)

    created = _dt(payload.get("date_created_gmt"))
    since = SyncState.load().orders_since

    with transaction.atomic():
        order = WebOrder.objects.select_for_update().filter(woo_id=woo_id).first()
        result = "updated"
        if order is None:
            if since and created and created < since:
                return None                                # placed before the sync was switched on
            order = WebOrder(woo_id=woo_id)
            result = "new"

        billing = payload.get("billing") or {}
        shipping = payload.get("shipping") or {}
        name = " ".join(x for x in (billing.get("first_name"), billing.get("last_name")) if x).strip()
        order.number = str(payload.get("number") or woo_id)[:40]
        order.status = status[:30]
        order.woo_created = created
        order.woo_modified = _dt(payload.get("date_modified_gmt"))
        order.customer_name = (name or str(billing.get("company") or ""))[:120]
        order.email = str(billing.get("email") or "")[:254]
        order.phone = str(billing.get("phone") or "")[:30]
        order.billing_address = _address(billing)
        order.shipping_address = _address(shipping)
        order.payment_method = str(payload.get("payment_method_title") or payload.get("payment_method") or "")[:80]
        order.payment_method_code = str(payload.get("payment_method") or "")[:80]
        order.transaction_id = str(payload.get("transaction_id") or "")[:100]
        order.is_paid = bool(payload.get("date_paid_gmt"))
        order.total = _money(payload.get("total"))
        order.currency = str(payload.get("currency") or "")[:8]
        order.financial_data = financial_data
        order.customer_note = str(payload.get("customer_note") or "")[:500]
        order.save()

        existing = {i.woo_line_id: i for i in order.items.select_for_update()}
        seen = set()
        for line in line_items:
            lid = line.get("id")
            if not isinstance(lid, int) or isinstance(lid, bool):
                continue
            seen.add(lid)
            item = existing.get(lid) or WebOrderItem(order=order, woo_line_id=lid)
            wid = line.get("variation_id") or line.get("product_id")
            item.woo_product_id = wid if isinstance(wid, int) and not isinstance(wid, bool) and wid > 0 else None
            item.name = str(line.get("name") or "")[:200]
            item.sku = str(line.get("sku") or "")[:100]
            item.quantity = _qty(line.get("quantity"))
            item.line_total = _money(line.get("total"))
            item.save()
            existing[lid] = item

        for lid, item in existing.items():                 # a line removed on the website
            if lid not in seen and item.quantity != 0:
                item.quantity = Decimal("0.000")
                item.save(update_fields=["quantity"])

        reconcile_stock(order)
    return result


def import_orders(client, state, stats):
    cursor = state.orders_cursor or state.orders_since
    params = {"orderby": "modified", "order": "asc", "per_page": 100, "dates_are_gmt": "true"}
    if cursor:
        params["modified_after"] = (cursor - OVERLAP).astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    newest, failed = None, False
    for payload in client.get_all("orders", params):
        if not isinstance(payload, dict):
            continue
        stats["orders_seen"] += 1
        try:
            result = apply_order(payload)
        except InvalidOrderPayload as exc:
            log.warning("Could not import website order %s: %s", payload.get("id"), exc)
            failed = True
            stats["orders_failed"] += 1
            if len(stats["errors"]) < ERROR_CAP:
                stats["errors"].append(f"Order #{payload.get('number', payload.get('id'))} could not be imported.")
            continue
        except Exception:
            log.exception("Could not import website order %s", payload.get("id"))
            failed = True
            stats["orders_failed"] += 1
            if len(stats["errors"]) < ERROR_CAP:
                stats["errors"].append(f"Order #{payload.get('number', payload.get('id'))} could not be imported.")
            continue
        if result == "new":
            stats["orders_new"] += 1
        elif result == "updated":
            stats["orders_updated"] += 1
        modified = _dt(payload.get("date_modified_gmt"))
        if modified and (newest is None or modified > newest):
            newest = modified

    if newest and not failed:                              # never move the bookmark past an order that failed
        SyncState.objects.filter(pk=1).update(orders_cursor=newest)


# ---------- Stock: POS -> website ----------
def planned_pushes(holdback=0):
    """[(link, product, target)] for every online product whose website stock differs from what we last sent."""
    qs = with_stock(
        Product.objects.select_related("woo_link").filter(
            woo_link__sell_online=True, woo_link__woo_product_id__isnull=False, track_stock=True,
        )
    ).order_by("pk")
    plan = []
    for p in qs:
        link = p.woo_link
        if link.woo_type != "simple":
            continue                                       # variations are not synced in this version
        target = 0 if not p.is_active else max(0, math.floor(p.stock) - holdback)
        if link.last_pushed_qty != target:
            plan.append((link, p, target))
    return plan


def push_stock(client, state, stats, allow_mass_zero=False):
    plan = planned_pushes(state.holdback_units)
    drops = [x for x in plan if x[2] == 0 and x[0].last_pushed_qty != 0]     # first-time zeros count too
    if len(drops) > MASS_ZERO_LIMIT and not allow_mass_zero:
        skip = {x[0].pk for x in drops}
        plan = [x for x in plan if x[0].pk not in skip]
        stats["warnings"].append(
            f"{len(drops)} products would go to 0 on the website, so they were NOT sent. Check the preview on the "
            "Website sync page, then press Sync now with the override if it is right."
        )

    for start in range(0, len(plan), BATCH):
        chunk = plan[start:start + BATCH]
        body = {"update": [{"id": link.woo_product_id, "manage_stock": True, "stock_quantity": target}
                           for link, _, target in chunk]}
        response = client.post("products/batch", body)
        results = {r.get("id"): r for r in (response.get("update") or []) if isinstance(r, dict)}
        for link, product, target in chunk:
            r = results.get(link.woo_product_id)
            if r is not None and "error" not in r:
                link.last_pushed_qty = target
                link.last_pushed_at = timezone.now()
                link.save(update_fields=["last_pushed_qty", "last_pushed_at"])
                stats["pushed"] += 1
            elif len(stats["errors"]) < ERROR_CAP:
                message = ((r or {}).get("error") or {}).get("message") or "the website did not confirm it"
                stats["errors"].append(f"{product.name}: {message}")


# ---------- Matching ----------
def match_products(client):
    """Links POS products to website products by SKU. It never switches 'Sell online' on."""
    items = client.get_all("products", {"per_page": 100, "_fields": "id,sku,name,type,status"})
    codes = {}
    for p in Product.objects.exclude(code__isnull=True).exclude(code=""):
        codes.setdefault(p.code.lower(), p)

    result = {"matched": 0, "unsupported": 0, "no_sku": [], "unknown_sku": []}
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("id"), int):
            continue
        woo_id = it["id"]
        name = str(it.get("name") or f"#{woo_id}")
        sku = str(it.get("sku") or "").strip()
        if not sku:
            result["no_sku"].append(name)
            continue
        product = codes.get(sku.lower())
        if product is None:
            result["unknown_sku"].append(f"{name} ({sku})")
            continue

        woo_type = str(it.get("type") or "")[:20]
        with transaction.atomic():
            WooLink.objects.filter(woo_product_id=woo_id).exclude(product=product).update(
                woo_product_id=None, last_pushed_qty=None, last_pushed_at=None
            )
            link, _ = WooLink.objects.get_or_create(product=product)
            if link.woo_product_id != woo_id:
                link.last_pushed_qty = None                # a different website product: everything is sent afresh
                link.last_pushed_at = None
            link.woo_product_id = woo_id
            link.woo_type = woo_type
            link.save()
        result["matched"] += 1
        if woo_type != "simple":
            result["unsupported"] += 1
    return result


# ---------- One full sync ----------
def _summary(stats):
    parts = [f"{stats['orders_new']} new orders, {stats['orders_updated']} updated, {stats['pushed']} stock updates."]
    if stats["orders_failed"]:
        parts.append(f"{stats['orders_failed']} orders failed to import.")
    if stats.get("products_created") or stats.get("products_updated"):
        parts.append(
            f"{stats['products_created']} products imported, {stats['products_updated']} products updated."
        )
    if stats.get("products_review"):
        parts.append(f"{stats['products_review']} products need review.")
    if stats.get("product_jobs_failed"):
        parts.append(f"{stats['product_jobs_failed']} product sync jobs will retry.")
    parts += stats["warnings"]
    parts += stats["errors"][:ERROR_CAP]
    return " ".join(parts)


def run_sync(trigger="scheduled", client=None, allow_mass_zero=False):
    """Orders first, then stock. Raises SyncOff or SyncBusy when it should not run."""
    now = timezone.now()
    with transaction.atomic():
        state = SyncState.objects.select_for_update().get_or_create(pk=1)[0]
        if not state.is_enabled:
            raise SyncOff("Website sync is switched off.")
        if state.running_since and now - state.running_since < STALE_LOCK:
            raise SyncBusy("A sync is already running.")
        state.running_since = now
        state.save(update_fields=["running_since"])

    run = SyncRun.objects.create(trigger=trigger[:10])
    stats = {
        "orders_seen": 0, "orders_new": 0, "orders_updated": 0, "orders_failed": 0,
        "pushed": 0, "errors": [], "warnings": [], "products_created": 0,
        "products_updated": 0, "products_archived": 0, "products_review": 0,
        "product_jobs_failed": 0,
    }
    try:
        client = client or WooClient()
        import_orders(client, SyncState.load(), stats)
        if not stats["errors"]:
            from .products import sync_product_catalog
            product_stats = sync_product_catalog(client, SyncState.load())
            stats["products_created"] = product_stats["created"]
            stats["products_updated"] = product_stats["updated"]
            stats["products_archived"] = product_stats["archived"]
            stats["products_review"] = product_stats["review"]
            stats["product_jobs_failed"] = product_stats["jobs_failed"]
            if stats["product_jobs_failed"]:
                stats["errors"].append(f"{stats['product_jobs_failed']} product sync jobs failed.")
        if not stats["errors"]:                            # never send stock when orders could not all be read
            push_stock(client, SyncState.load(), stats, allow_mass_zero)
    except WooError as exc:
        stats["errors"].append(str(exc))
    except Exception:
        log.exception("Website sync failed")
        stats["errors"].append("Something unexpected went wrong. The details are in the server log.")

    ok = not stats["errors"]
    summary = _summary(stats)
    finished = timezone.now()

    run.finished_at = finished
    run.ok = ok
    run.orders_seen, run.orders_new = stats["orders_seen"], stats["orders_new"]
    run.orders_failed = stats["orders_failed"]
    run.orders_updated, run.stock_pushed = stats["orders_updated"], stats["pushed"]
    run.summary = summary
    run.save()

    updates = {"running_since": None, "last_run_at": finished, "last_ok": ok, "last_message": summary[:300]}
    if ok:
        updates["last_ok_at"] = finished
    SyncState.objects.filter(pk=1).update(**updates)
    return run