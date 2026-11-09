from django.db.models import Q
from django.utils import timezone

from inventory.models import Product
from .models import PromotionItem


def div_round(a, b):
    """a / b rounded half-up, for non-negative whole numbers."""
    return (2 * a + b) // (2 * b)


def best_discount(rules, price, qty_milli):
    """
    rules: [{"name", "kind", "value", "min"}]  value = basis points for percent, paise per unit for amount; min = milli-units
    price: paise per unit.  Returns (discount in paise, promotion name).  The best single rule wins; they never stack.
    """
    gross = div_round(price * qty_milli, 1000)
    best, name = 0, ""
    for r in rules:
        if r["min"] and qty_milli < r["min"]:
            continue
        if r["kind"] == "percent":
            d = div_round(gross * r["value"], 10000)
        else:
            d = div_round(r["value"] * qty_milli, 1000)
        d = min(d, gross)
        if d > best:
            best, name = d, r["name"]
    return best, name


def rules_for(product_ids, now=None):
    """The promotion rules that are running right now, per product: {product_id: [rule, ...]}."""
    ids = list(product_ids)
    if not ids:
        return {}
    product_targets = {
        row["pk"]: row for row in Product.objects.filter(pk__in=ids).values("pk", "category_id", "brand_id")
    }
    category_products, brand_products = {}, {}
    for product_id, target in product_targets.items():
        if target["category_id"]:
            category_products.setdefault(target["category_id"], []).append(product_id)
        if target["brand_id"]:
            brand_products.setdefault(target["brand_id"], []).append(product_id)

    now = now or timezone.now()
    today = timezone.localtime(now).weekday()

    items = (
        PromotionItem.objects.filter(
            Q(product_id__in=ids)
            | Q(category_id__in=category_products)
            | Q(brand_id__in=brand_products),
            promotion__is_active=True,
        )
        .filter(Q(promotion__starts_at__isnull=True) | Q(promotion__starts_at__lte=now))
        .filter(Q(promotion__ends_at__isnull=True) | Q(promotion__ends_at__gte=now))
        .select_related("promotion")
        .order_by("promotion_id", "id")
    )
    out = {}
    for it in items:
        days = it.promotion.weekday_set()
        if days and today not in days:
            continue
        rule = {
            "name": it.promotion.name,
            "kind": it.kind,
            "value": int((it.value * 100).to_integral_value()),
            "min": int((it.min_qty * 1000).to_integral_value()) if it.min_qty else 0,
        }
        if it.product_id:
            target_products = [it.product_id]
        elif it.category_id:
            target_products = category_products.get(it.category_id, [])
        else:
            target_products = brand_products.get(it.brand_id, [])
        for product_id in target_products:
            out.setdefault(product_id, []).append(rule)
    return out