from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from inventory.models import Category, Product, StockMovement

from .models import ProductSyncJob, WooLink
from .products import PRODUCT_FIELDS, enqueue_product_job


LOCAL_FIELDS = {
    "name": "name",
    "code": "sku",
    "mrp": "regular_price",
    "sale_price": "sale_price",
    "description": "description",
    "short_description": "short_description",
    "is_active": "status",
    "woo_tax_class": "tax_class",
    "weight": "weight",
    "dimensions": "dimensions",
    "attributes": "attributes",
    "category_id": "categories",
    "woo_tags": "tags",
    "woo_images": "images",
}


@receiver(pre_save, sender=Product)
def capture_product_changes(sender, instance, **kwargs):
    if not instance.pk:
        instance._woo_changed_fields = list(PRODUCT_FIELDS) + ["stock"]
        return
    old = sender.objects.filter(pk=instance.pk).values().first()
    if old is None:
        instance._woo_changed_fields = list(PRODUCT_FIELDS) + ["stock"]
        return
    changed = {
        LOCAL_FIELDS[field]
        for field in LOCAL_FIELDS
        if old.get(field) != getattr(instance, field)
    }
    instance._woo_changed_fields = sorted(changed)


@receiver(post_save, sender=Product)
def enqueue_product_change(sender, instance, created, **kwargs):
    if getattr(instance, "_sync_origin", None) == "woocommerce":
        return
    link = WooLink.objects.filter(product_id=instance.pk).first()
    if link is None or not link.sell_online:
        return
    fields = getattr(instance, "_woo_changed_fields", None)
    if fields:
        event_type = "created" if created else "updated"
        transaction.on_commit(
            lambda product_id=instance.pk, event_type=event_type, fields=fields:
                enqueue_product_job(Product.objects.get(pk=product_id), event_type, fields)
        )


@receiver(pre_delete, sender=Product)
def preserve_woo_product_reference(sender, instance, **kwargs):
    link = WooLink.objects.filter(product=instance).first()
    instance._woo_deleted_id = link.woo_product_id if link else None


@receiver(post_delete, sender=Product)
def enqueue_woo_product_archive(sender, instance, **kwargs):
    woo_id = getattr(instance, "_woo_deleted_id", None)
    if not woo_id:
        return
    transaction.on_commit(
        lambda: ProductSyncJob.objects.create(
            woo_product_id=woo_id,
            direction="pos",
            event_type="deleted",
            idempotency_key=f"pos-delete:{woo_id}:{instance.pk}",
        )
    )


@receiver(post_save, sender=StockMovement)
def enqueue_stock_change(sender, instance, created, **kwargs):
    if not created or instance.source == StockMovement.Source.WOOCOMMERCE:
        return
    if not WooLink.objects.filter(product_id=instance.product_id, sell_online=True).exists():
        return
    transaction.on_commit(
        lambda product_id=instance.product_id:
            enqueue_product_job(Product.objects.get(pk=product_id), "stock", ["stock"])
    )


@receiver(post_save, sender=Category)
def enqueue_category_change(sender, instance, created, **kwargs):
    if not instance.pk or getattr(instance, "_sync_origin", None) == "woocommerce":
        return
    product_ids = list(
        Product.objects.filter(category=instance, woo_link__sell_online=True).values_list("pk", flat=True)
    )
    for product_id in product_ids:
        transaction.on_commit(
            lambda product_id=product_id:
                enqueue_product_job(Product.objects.get(pk=product_id), "updated", ["categories"])
        )
