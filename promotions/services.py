from decimal import Decimal, InvalidOperation

from django.db import transaction

from inventory.models import Brand, Category, Product

from .models import PromotionItem

MAX_ROWS = 200
MAX_QTY = Decimal("1000000")
COUNT_UNITS = {"pcs", "set", "box", "pack"}   # same list as pos.services (copied here to avoid a circular import)
TWO = Decimal("0.01")
THREE = Decimal("0.001")


class PromotionError(Exception):
    pass


def _decimal(value, label):
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, TypeError):
        raise PromotionError(f"{label} is not a valid number.")
    if not d.is_finite():
        raise PromotionError(f"{label} is not a valid number.")
    return d


def parse_items(post):
    """Reads product, category, or brand targets and their discount rows."""
    target_types = post.getlist("target_type")
    target_ids = post.getlist("target_id")
    if not target_types:
        target_types = ["product"] * len(post.getlist("product"))
        target_ids = post.getlist("product")
    if len(target_types) != len(target_ids):
        raise PromotionError("A promotion target is invalid.")

    picked = []
    for index, (target_type, raw_id) in enumerate(zip(target_types, target_ids)):
        target_id = (raw_id or "").strip()
        if not target_id:
            continue
        if target_type not in {"product", "category", "brand"} or not target_id.isdigit():
            raise PromotionError("A promotion target is invalid.")
        picked.append((index, target_type, int(target_id)))

    if not picked:
        raise PromotionError("Add at least one product, category, or brand to the promotion.")
    if len(picked) > MAX_ROWS:
        raise PromotionError(f"A promotion can have at most {MAX_ROWS} targets.")

    product_ids = [target_id for _, target_type, target_id in picked if target_type == "product"]
    category_ids = [target_id for _, target_type, target_id in picked if target_type == "category"]
    brand_ids = [target_id for _, target_type, target_id in picked if target_type == "brand"]
    products = {p.pk: p for p in Product.objects.filter(pk__in=product_ids, is_active=True)}
    categories = {c.pk: c for c in Category.objects.filter(pk__in=category_ids, is_active=True)}
    brands = {b.pk: b for b in Brand.objects.filter(pk__in=brand_ids, is_active=True)}
    rows, seen = [], set()

    for index, target_type, target_id in picked:
        target = {"product": products, "category": categories, "brand": brands}[target_type].get(target_id)
        if target is None:
            raise PromotionError(f"The selected {target_type} #{target_id} is no longer available.")
        if target_type == "product":
            target_products = [target]
            label = target.name
        elif target_type == "category":
            target_products = list(Product.objects.filter(category=target, is_active=True))
            label = f"category {target.name}"
        else:
            target_products = list(Product.objects.filter(brand=target, is_active=True))
            label = f"brand {target.name}"

        kind = post.get(f"kind_{index}")
        if kind not in PromotionItem.Kind.values:
            raise PromotionError(f"Choose a discount type for {label}.")

        value = _decimal(post.get(f"value_{index}"), f"Discount for {label}")
        if value != value.quantize(TWO):
            raise PromotionError(f"Discount for {label} can have at most 2 decimal places.")
        if kind == PromotionItem.Kind.PERCENT:
            if value <= 0 or value > 100:
                raise PromotionError(f"Percent for {label} must be above 0 and at most 100.")
        else:
            if value <= 0:
                raise PromotionError(f"Amount off for {label} must be above zero.")
            if target_products and any(value > p.sale_price for p in target_products):
                raise PromotionError(f"Amount off for {label} cannot exceed the price of any active target product.")

        min_qty = None
        raw = (post.get(f"min_qty_{index}") or "").strip()
        if raw:
            d = _decimal(raw, f"Minimum quantity for {label}")
            if d <= 0 or d > MAX_QTY:
                raise PromotionError(f"Minimum quantity for {label} must be above zero.")
            if d != d.quantize(THREE):
                raise PromotionError(f"Minimum quantity for {label} can have at most 3 decimal places.")
            if any(p.unit in COUNT_UNITS for p in target_products) and d != d.to_integral_value():
                raise PromotionError(f"Products in {label} use whole-number quantities, so the minimum must be a whole number.")
            min_qty = d.quantize(THREE)

        key = (target_type, target_id, min_qty)
        if key in seen:
            raise PromotionError(f"{label} is listed twice with the same quantity condition.")
        seen.add(key)
        rows.append({
            "target_type": target_type, "target_id": target_id, "target": target,
            "product": target if target_type == "product" else None,
            "category": target if target_type == "category" else None,
            "brand": target if target_type == "brand" else None,
            "kind": kind, "value": value.quantize(TWO), "min_qty": min_qty,
        })

    return rows


def save_promotion(promotion, rows):
    with transaction.atomic():
        promotion.save()
        promotion.items.all().delete()
        PromotionItem.objects.bulk_create([
            PromotionItem(
                promotion=promotion, product=r["product"], category=r["category"], brand=r["brand"],
                kind=r["kind"], value=r["value"], min_qty=r["min_qty"],
            )
            for r in rows
        ])
    return promotion