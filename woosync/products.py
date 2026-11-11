import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from inventory.models import Category, Product, ProductImage, ProductTag, StockMovement
from inventory.stock import current_stock

from .client import WooError
from .models import ProductSyncConflict, ProductSyncJob, WooLink

log = logging.getLogger("woosync")
PRODUCT_FIELDS = (
    "name", "sku", "regular_price", "sale_price", "description", "short_description",
    "status", "tax_class", "weight", "dimensions", "attributes", "categories", "tags", "images",
)


def _date(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


def _decimal(value, label):
    try:
        result = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        raise WooError(f"WooCommerce returned an invalid {label}.") from None
    if not result.is_finite() or result < 0:
        raise WooError(f"WooCommerce returned an invalid {label}.")
    return result.quantize(Decimal("0.01"))


def _tags_payload(product):
    return [
        {"id": item["id"]} if item.get("id") else {"name": item["name"]}
        for item in product.woo_tags
        if isinstance(item, dict) and item.get("id", item.get("name"))
    ] if isinstance(product.woo_tags, list) else []


def _snapshot(product):
    return {
        "name": product.name,
        "sku": product.code or "",
        "regular_price": str(product.mrp or product.sale_price),
        "sale_price": str(product.sale_price) if product.sale_price < product.mrp else "",
        "description": product.description,
        "short_description": product.short_description,
        "tax_class": product.woo_tax_class,
        "weight": product.weight,
        "dimensions": product.dimensions if isinstance(product.dimensions, dict) else {},
        "attributes": product.attributes if isinstance(product.attributes, list) else [],
        "status": "publish" if product.is_active else (
            product.woo_status if product.woo_status in ("draft", "private", "pending") else "draft"
        ),
        "category_id": product.category_id,
        "tags": product.woo_tags if isinstance(product.woo_tags, list) else [],
    }


def enqueue_product_job(
    product, event_type="updated", fields=None, force_fields=None, allow_offline=False
):
    key = f"pos:{product.pk}:{uuid.uuid4()}"
    payload = {"fields": list(fields or PRODUCT_FIELDS)}
    if force_fields:
        payload["force_fields"] = list(force_fields)
    if allow_offline:
        payload["allow_offline"] = True
    ProductSyncJob.objects.create(
        product=product,
        direction="pos",
        event_type=event_type,
        idempotency_key=key,
        payload=payload,
    )


def enqueue_webhook_job(woo_id, event_type, payload, delivery_id):
    key = f"webhook:{delivery_id}" if delivery_id else f"webhook:{event_type}:{woo_id}:{uuid.uuid4()}"
    return ProductSyncJob.objects.get_or_create(
        idempotency_key=key,
        defaults={
            "woo_product_id": woo_id,
            "direction": "woocommerce",
            "event_type": event_type,
            "payload": payload,
        },
    )


def _category_rows(client):
    return client.get_all("products/categories", {"hide_empty": "false", "per_page": 100})


def sync_categories(client):
    rows = _category_rows(client)
    categories = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), int):
            continue
        item = Category.objects.filter(woo_id=row["id"]).first()
        if item is None:
            item = Category.objects.filter(name__iexact=str(row.get("name") or "")[:100]).first()
        if item is None:
            name = str(row.get("name") or f"Woo category {row['id']}")[:100]
            item = Category.objects.create(name=name, woo_id=row["id"])
        else:
            item.name = str(row.get("name") or item.name)[:100]
            item.description = str(row.get("description") or "")[:5000]
            item.woo_id = row["id"]
            item._sync_origin = "woocommerce"
            item.save(update_fields=["name", "description", "woo_id", "updated_at"])
        categories[row["id"]] = (item, row.get("parent") or None)
    for woo_id, (item, parent_id) in categories.items():
        parent = categories.get(parent_id, (None, None))[0]
        if item.parent_id != getattr(parent, "pk", None):
            item.parent = parent
            item._sync_origin = "woocommerce"
            item.save(update_fields=["parent", "updated_at"])
    return categories


def sync_tags(client):
    tags = client.get_all("products/tags", {"hide_empty": "false", "per_page": 100})
    by_id, by_name = {}, {}
    for row in tags:
        if not isinstance(row, dict) or not isinstance(row.get("id"), int):
            continue
        name = str(row.get("name") or "")[:100]
        tag = ProductTag.objects.filter(woo_id=row["id"]).first()
        if tag is None:
            tag = ProductTag.objects.filter(name__iexact=name).first()
        if tag is None:
            tag = ProductTag(name=name, woo_id=row["id"])
        else:
            tag.name = name
            tag.woo_id = row["id"]
        tag.description = str(row.get("description") or "")[:5000]
        tag.save()
        by_id[row["id"]] = tag
        by_name[name.casefold()] = tag
    return by_id, by_name


def _remote_snapshot(data):
    return {
        "name": str(data.get("name") or ""),
        "sku": str(data.get("sku") or ""),
        "regular_price": str(data.get("regular_price") or ""),
        "sale_price": str(data.get("sale_price") or ""),
        "description": str(data.get("description") or ""),
        "short_description": str(data.get("short_description") or ""),
        "status": str(data.get("status") or "draft"),
        "tax_class": str(data.get("tax_class") or ""),
        "weight": str(data.get("weight") or ""),
        "dimensions": data.get("dimensions") or {},
        "attributes": data.get("attributes") or [],
        "categories": [c.get("id") for c in data.get("categories", []) if isinstance(c, dict)],
        "tags": [
            {"id": t.get("id"), "name": str(t.get("name") or "")}
            for t in data.get("tags", []) if isinstance(t, dict)
        ],
        "images": [
            {"id": i.get("id"), "src": str(i.get("src") or ""), "alt": str(i.get("alt") or "")}
            for i in data.get("images", []) if isinstance(i, dict)
        ],
    }


def _equal(field, left, right):
    if field in ("regular_price", "sale_price"):
        try:
            return Decimal(str(left or "0")) == Decimal(str(right or "0"))
        except InvalidOperation:
            return left == right
    return left == right


def _detect_conflicts(product, link, data):
    if not product or not link or not link.last_synced_data:
        return []
    baseline = link.last_synced_data
    remote = _remote_snapshot(data)
    local = _snapshot(product)
    conflicts = []
    for field in (
        "name", "sku", "regular_price", "sale_price", "description", "short_description",
        "status", "tax_class", "weight", "dimensions", "attributes",
    ):
        if field not in baseline:
            continue
        if (
            not _equal(field, local[field], baseline[field])
            and not _equal(field, remote[field], baseline[field])
            and not _equal(field, local[field], remote[field])
        ):
            conflicts.append(field)
            _save_conflict(product, field, local[field], remote[field])
            data[field] = local[field]
    return conflicts


def _save_conflict(product, field, pos_value, woo_value):
    ProductSyncConflict.objects.get_or_create(
        product=product,
        field_name=field,
        resolved_at__isnull=True,
        defaults={"pos_value": pos_value, "woo_value": woo_value},
    )


def _queue_review(woo_id, reason, payload):
    key = f"review:{woo_id}:{hashlib.sha256(reason.encode()).hexdigest()[:16]}"
    ProductSyncJob.objects.get_or_create(
        idempotency_key=key,
        defaults={
            "woo_product_id": woo_id,
            "direction": "woocommerce",
            "event_type": "review",
            "payload": {"reason": reason, "product": payload},
            "status": ProductSyncJob.Status.REVIEW,
            "last_error": reason,
        },
    )


def _map_categories(data, categories):
    ids = [row.get("id") for row in data.get("categories", []) if isinstance(row, dict) and row.get("id")]
    primary = categories.get(ids[0], (None, None))[0] if ids else None
    return ids, primary


def _apply_woo_stock(product, data, woo_id):
    if not product.track_stock or not data.get("manage_stock") or data.get("stock_quantity") is None:
        return
    quantity = Decimal(str(data["stock_quantity"]))
    if not quantity.is_finite() or quantity < 0:
        raise WooError("WooCommerce returned an invalid stock quantity.")
    delta = quantity - current_stock(product)
    if delta:
        StockMovement.objects.create(
            product=product,
            quantity=delta,
            movement_type=StockMovement.Type.ADJUSTMENT,
            source=StockMovement.Source.WOOCOMMERCE,
            reference=f"WC-P-{woo_id}"[:50],
            note=f"WooCommerce stock reconciliation for product #{woo_id}"[:255],
        )


def sync_product_from_woo(data, categories=None, tags=None):
    if not isinstance(data, dict) or not isinstance(data.get("id"), int):
        raise WooError("WooCommerce returned an invalid product.")
    data = dict(data)
    original_remote_snapshot = _remote_snapshot(data)
    woo_id = data["id"]
    status = str(data.get("status") or "")
    if status == "trash":
        link = WooLink.objects.filter(woo_product_id=woo_id).select_related("product").first()
        if link:
            link.product.is_active = False
            link.product.woo_status = "trash"
            link.product._sync_origin = "woocommerce"
            link.product.save(update_fields=["is_active", "woo_status", "updated_at"])
            link.sync_status = "synced"
            link.last_synced_at = timezone.now()
            link.save(update_fields=["sync_status", "last_synced_at"])
        return "archived"
    if str(data.get("type") or "simple") != "simple":
        _queue_review(woo_id, f"Unsupported WooCommerce product type: {data.get('type')}", data)
        return "review"

    sku = str(data.get("sku") or "").strip()
    link = WooLink.objects.filter(woo_product_id=woo_id).select_related("product").first()
    product = link.product if link else None
    modified = _date(data.get("date_modified_gmt"))
    if link and link.woo_modified and modified and modified < link.woo_modified:
        return "updated"
    if product is None and sku:
        product = Product.objects.filter(code__iexact=sku).first()
    if product:
        other = WooLink.objects.filter(product=product).exclude(woo_product_id=woo_id).first()
        if other:
            _queue_review(woo_id, f"SKU {sku!r} is already mapped to WooCommerce product #{other.woo_product_id}.", data)
            return "review"
    if len(sku) > 30:
        _queue_review(woo_id, "WooCommerce SKU is longer than the POS SKU field allows.", data)
        return "review"
    if sku and Product.objects.filter(code__iexact=sku).exclude(pk=getattr(product, "pk", None)).exists():
        _queue_review(woo_id, f"SKU {sku!r} is already assigned to another POS product.", data)
        return "review"
    conflict_fields = []
    if product:
        remote_values = _remote_snapshot(data)
        for conflict in ProductSyncConflict.objects.filter(product=product, resolved_at__isnull=True):
            field = conflict.field_name
            if field in remote_values and _equal(field, remote_values[field], conflict.pos_value):
                conflict.resolved_at = timezone.now()
                conflict.resolution = "matched"
                conflict.save(update_fields=["resolved_at", "resolution"])
            elif field in remote_values:
                data[field] = conflict.pos_value
                conflict_fields.append(field)
        conflict_fields.extend(_detect_conflicts(product, link, data))
        conflict_fields = list(dict.fromkeys(conflict_fields))

    categories = categories or {}
    tags = tags or {}
    category_ids, category = _map_categories(data, categories)
    regular = _decimal(data.get("regular_price") or data.get("price"), "regular price")
    sale = _decimal(data.get("sale_price") or regular, "sale price")
    if sale > regular:
        _queue_review(woo_id, "WooCommerce sale price exceeds the regular price.", data)
        return "review"

    created = product is None
    product = product or Product(name=str(data.get("name") or f"Woo product {woo_id}")[:200])
    product.name = str(data.get("name") or product.name)[:200]
    product.code = sku or product.code
    product.mrp = regular
    product.sale_price = sale
    product.description = str(data.get("description") or "")[:20000]
    product.short_description = str(data.get("short_description") or "")[:5000]
    product.weight = str(data.get("weight") or "")[:30]
    product.dimensions = {
        key: str((data.get("dimensions") or {}).get(key) or "")
        for key in ("length", "width", "height")
    }
    product.attributes = data.get("attributes") or []
    product.woo_tax_class = str(data.get("tax_class") or "")[:100]
    product.woo_status = status[:20]
    product.is_active = status == "publish"
    product.category = category
    product.woo_tags = [
        {"id": row.get("id"), "name": str(row.get("name") or "")}
        for row in data.get("tags", []) if isinstance(row, dict)
    ]
    product.woo_images = [
        {"id": row.get("id"), "src": str(row.get("src") or ""), "alt": str(row.get("alt") or "")}
        for row in data.get("images", []) if isinstance(row, dict)
    ]
    product._sync_origin = "woocommerce"
    product.save()

    if link is None:
        link = WooLink(product=product, woo_product_id=woo_id)
    link.woo_type = "simple"
    link.sell_online = status == "publish"
    link.woo_category_ids = category_ids
    link.woo_image_ids = [r.get("id") for r in data.get("images", []) if isinstance(r, dict) and r.get("id")]
    link.woo_modified = _date(data.get("date_modified_gmt"))
    link.last_synced_at = timezone.now()
    link.last_synced_data = original_remote_snapshot
    link.sync_status = "review" if conflict_fields else "synced"
    link.sync_error = ""
    link.needs_review = bool(conflict_fields)
    link.save()

    product.tags.set([
        tags[tag["id"]]
        for tag in product.woo_tags
        if tag.get("id") in tags
    ])
    _apply_woo_stock(product, data, woo_id)
    return "created" if created else "updated"


def _ensure_category(client, category, ancestors=None):
    if category is None:
        return None
    if category.woo_id:
        return category.woo_id
    ancestors = set() if ancestors is None else ancestors
    if category.pk in ancestors:
        raise WooError("A product category has a circular parent relationship.")
    ancestors.add(category.pk)
    parent_id = _ensure_category(client, category.parent, ancestors) if category.parent_id else None
    payload = {"name": category.name, "description": category.description}
    if parent_id:
        payload["parent"] = parent_id
    response = client.post("products/categories", payload)
    category.woo_id = response["id"]
    category._sync_origin = "woocommerce"
    category.save(update_fields=["woo_id", "updated_at"])
    return category.woo_id


def _ensure_tags(client, product):
    tags = []
    for item in product.woo_tags if isinstance(product.woo_tags, list) else []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        tag = ProductTag.objects.filter(woo_id=item.get("id")).first() if item.get("id") else None
        tag = tag or ProductTag.objects.filter(name__iexact=item["name"]).first()
        if tag is None or tag.woo_id is None:
            created = client.post("products/tags", {"name": item["name"]})
            tag = tag or ProductTag(name=item["name"])
            tag.woo_id = created["id"]
            tag.name = created.get("name") or item["name"]
            tag.save()
        tags.append({"id": tag.woo_id})
    return tags


def _image_payload(client, product, link):
    images = []
    files = []
    if product.image:
        files.append((product.image, product.image.name))
    for gallery in product.images.all():
        files.append((gallery.image, gallery.image.name))

    hashes = []
    old_hashes = link.last_synced_data.get("image_hashes", [])
    media_ids = list(link.woo_image_ids or [])[:len(files)]
    if not files and old_hashes:
        link.woo_image_ids = []
        link.last_synced_data["image_hashes"] = []
        link.save(update_fields=["woo_image_ids", "last_synced_data"])
        return []
    for index, (image, name) in enumerate(files):
        try:
            with image.open("rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
        except (OSError, ValueError):
            raise WooError("A local product image could not be read.") from None
        hashes.append(digest)
        if index < len(media_ids) and index < len(old_hashes) and old_hashes[index] == digest:
            media_id = media_ids[index]
        else:
            if not hasattr(image, "path"):
                raise WooError("The configured media storage cannot upload local product images.")
            uploaded = client.upload_media(image.path, name.replace("\\", "/").rsplit("/", 1)[-1])
            media_id = uploaded["id"]
            if index < len(media_ids):
                media_ids[index] = media_id
            else:
                media_ids.append(media_id)
            link.woo_image_ids = media_ids
            link.last_synced_data["image_hashes"] = hashes
            link.save(update_fields=["woo_image_ids", "last_synced_data"])
        images.append({"id": media_id})
    link.woo_image_ids = media_ids
    link.last_synced_data["image_hashes"] = hashes
    if not images:
        images = [{"id": media_id} for media_id in media_ids]
    return images


def _pos_payload(client, product, link, fields=None):
    fields = set(fields or PRODUCT_FIELDS)
    category_id = _ensure_category(client, product.category) if "categories" in fields else None
    payload = {}
    values = {
        "name": product.name,
        "sku": product.code or "",
        "regular_price": str(product.mrp or product.sale_price),
        "sale_price": str(product.sale_price) if product.sale_price < product.mrp else "",
        "description": product.description,
        "short_description": product.short_description,
        "status": "publish" if product.is_active else (
            product.woo_status if product.woo_status in ("draft", "private", "pending") else "draft"
        ),
        "tax_class": product.woo_tax_class,
        "weight": product.weight,
        "dimensions": product.dimensions or {},
        "attributes": product.attributes or [],
    }
    payload.update({key: value for key, value in values.items() if key in fields})
    if "categories" in fields:
        payload["categories"] = [{"id": category_id}] if category_id else []
    if "tags" in fields:
        payload["tags"] = _ensure_tags(client, product)
    if "images" in fields:
        payload["images"] = _image_payload(client, product, link)
    if "stock" in fields and product.track_stock:
        from inventory.stock import current_stock
        payload.update(manage_stock=True, stock_quantity=max(0, int(current_stock(product))))
    return payload


def _run_pos_job(client, job):
    if job.event_type == "deleted":
        if job.woo_product_id:
            client.put(f"products/{job.woo_product_id}", {"status": "draft"})
        return
    if job.product_id is None:
        return
    product = Product.objects.select_related("category", "category__parent").get(pk=job.product_id)
    link = WooLink.objects.select_for_update().filter(product=product).first()
    if not job.payload.get("allow_offline") and (link is None or not link.sell_online):
        return
    if link is None:
        link = WooLink.objects.create(product=product, sync_status="pending")
    fields = job.payload.get("fields") or PRODUCT_FIELDS
    has_conflicts = False
    if link.woo_product_id and link.last_synced_data:
        remote, _ = client.get_page(f"products/{link.woo_product_id}")
        remote_snapshot = _remote_snapshot(remote)
        local_snapshot = _snapshot(product)
        baseline = link.last_synced_data
        force_fields = set(job.payload.get("force_fields") or [])
        outgoing = {
            "sku": "sku", "name": "name", "regular_price": "regular_price",
            "sale_price": "sale_price", "description": "description",
            "short_description": "short_description", "status": "status",
            "tax_class": "tax_class", "weight": "weight", "dimensions": "dimensions",
            "attributes": "attributes",
        }
        allowed = []
        for field in fields:
            if field not in outgoing:
                allowed.append(field)
                continue
            remote_field = outgoing[field]
            if remote_field in baseline and (
                remote_field not in force_fields
                and
                not _equal(remote_field, local_snapshot[remote_field], baseline[remote_field])
                and not _equal(remote_field, remote_snapshot[remote_field], baseline[remote_field])
                and not _equal(remote_field, local_snapshot[remote_field], remote_snapshot[remote_field])
            ):
                _save_conflict(
                    product, remote_field, local_snapshot[remote_field], remote_snapshot[remote_field]
                )
                has_conflicts = True
            else:
                allowed.append(field)
        fields = allowed
    if not fields:
        link.needs_review = has_conflicts
        link.sync_status = "review" if has_conflicts else "synced"
        link.sync_error = ""
        link.save(update_fields=["needs_review", "sync_status", "sync_error"])
        return
    payload = _pos_payload(client, product, link, fields)
    if link.woo_product_id is None:
        existing = client.get_all("products", {"sku": product.code or "", "per_page": 100}) if product.code else []
        matching = [row for row in existing if isinstance(row, dict) and row.get("sku") == product.code]
        if len(matching) > 1 or (matching and str(matching[0].get("name") or "").casefold() != product.name.casefold()):
            raise WooError("A WooCommerce product with this SKU exists but does not match; review the mapping.")
        if matching:
            response = matching[0]
            link.woo_product_id = response["id"]
            link.woo_type = response.get("type", "simple")
            response = client.put(
                f"products/{link.woo_product_id}", _pos_payload(client, product, link)
            )
        else:
            response = client.post("products", _pos_payload(client, product, link))
            link.woo_product_id = response["id"]
            link.woo_type = response.get("type", "simple")
    else:
        response = client.put(f"products/{link.woo_product_id}", payload)
    image_hashes = link.last_synced_data.get("image_hashes", [])
    link.woo_modified = _date(response.get("date_modified_gmt"))
    link.last_synced_at = timezone.now()
    link.last_synced_data = _remote_snapshot(response)
    if image_hashes:
        link.last_synced_data["image_hashes"] = image_hashes
    link.sync_status = "review" if has_conflicts else "synced"
    link.sync_error = ""
    link.needs_review = has_conflicts
    link.save()


def _run_woo_job(client, job):
    if job.event_type == "deleted":
        link = WooLink.objects.filter(woo_product_id=job.woo_product_id).select_related("product").first()
        if link:
            product = link.product
            product.is_active = False
            product.woo_status = "trash"
            product._sync_origin = "woocommerce"
            product.save(update_fields=["is_active", "woo_status", "updated_at"])
            link.sync_status = "synced"
            link.needs_review = False
            link.last_synced_at = timezone.now()
            link.save(update_fields=["sync_status", "needs_review", "last_synced_at"])
        return
    data, _ = client.get_page(f"products/{job.woo_product_id}")
    link = WooLink.objects.filter(woo_product_id=job.woo_product_id).select_related("product").first()
    force_fields = set(job.payload.get("force_fields") or [])
    if link and force_fields:
        baseline = dict(link.last_synced_data)
        local = _snapshot(link.product)
        for field in force_fields:
            if field in local:
                baseline[field] = local[field]
        link.last_synced_data = baseline
        link.save(update_fields=["last_synced_data"])
    categories = {
        c.woo_id: (c, c.parent_id) for c in Category.objects.exclude(woo_id__isnull=True)
    }
    tags = {t.woo_id: t for t in ProductTag.objects.exclude(woo_id__isnull=True)}
    sync_product_from_woo(data, categories, tags)


def process_product_jobs(client, limit=100):
    counts = {"processed": 0, "failed": 0}
    ProductSyncJob.objects.filter(
        status=ProductSyncJob.Status.PROCESSING,
        updated_at__lt=timezone.now() - timedelta(minutes=10),
    ).update(status=ProductSyncJob.Status.PENDING, available_at=timezone.now())
    for job_id in ProductSyncJob.objects.filter(
        status=ProductSyncJob.Status.PENDING, available_at__lte=timezone.now()
    ).order_by("created_at", "id").values_list("pk", flat=True)[:limit]:
        with transaction.atomic():
            job = ProductSyncJob.objects.select_for_update().filter(
                pk=job_id, status=ProductSyncJob.Status.PENDING
            ).first()
            if job is None:
                continue
            job.status = ProductSyncJob.Status.PROCESSING
            job.attempts += 1
            job.save(update_fields=["status", "attempts", "updated_at"])
        try:
            with transaction.atomic():
                locked = ProductSyncJob.objects.select_for_update().get(pk=job_id)
                if locked.direction == "pos":
                    _run_pos_job(client, locked)
                else:
                    _run_woo_job(client, locked)
                locked.status = ProductSyncJob.Status.DONE
                locked.last_error = ""
                locked.save(update_fields=["status", "last_error", "updated_at"])
        except Exception as exc:
            delay = min(3600, 5 * (2 ** min(job.attempts - 1, 10)))
            ProductSyncJob.objects.filter(pk=job_id).update(
                status=ProductSyncJob.Status.FAILED if job.attempts >= 10 else ProductSyncJob.Status.PENDING,
                available_at=timezone.now() + timedelta(seconds=delay),
                last_error=str(exc)[:2000],
            )
            if job.product_id:
                WooLink.objects.filter(product_id=job.product_id).update(
                    sync_status="failed", sync_error=str(exc)[:2000]
                )
            log.exception("Product sync job %s failed", job_id)
            counts["failed"] += 1
        counts["processed"] += 1
    return counts


def import_products(client, state=None):
    categories = sync_categories(client)
    tags_by_id, _ = sync_tags(client)
    params = {"per_page": 100, "status": "any", "orderby": "modified", "order": "asc"}
    if state and state.products_cursor:
        params["modified_after"] = (state.products_cursor - timedelta(minutes=5)).astimezone(
            dt_timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S")
    products = client.get_all("products", params)
    newest = state.products_cursor if state else None
    counts = {"created": 0, "updated": 0, "archived": 0, "review": 0}
    for row in products:
        if not isinstance(row, dict) or not isinstance(row.get("id"), int):
            continue
        modified = _date(row.get("date_modified_gmt"))
        if modified and (newest is None or modified > newest):
            newest = modified
        result = sync_product_from_woo(row, categories, tags_by_id)
        if result in counts:
            counts[result] += 1
    if state and newest:
        state.products_cursor = newest
        state.save(update_fields=["products_cursor"])
    return counts


def sync_product_catalog(client, state=None):
    jobs = process_product_jobs(client)
    imported = import_products(client, state)
    return {**imported, **{f"jobs_{key}": value for key, value in jobs.items()}}
