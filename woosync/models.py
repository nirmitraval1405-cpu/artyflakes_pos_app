from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel
from inventory.models import Product

HOLDING = ("processing", "on-hold", "completed")        # website order statuses that take stock out
CLOSED = ("cancelled", "refunded", "failed", "trash")
MONEY = {"max_digits": 14, "decimal_places": 2}


class SyncState(models.Model):
    """One row (pk=1): the on/off switch, the settings and the bookmarks of the website sync."""
    is_enabled = models.BooleanField(default=False)
    holdback_units = models.PositiveIntegerField(default=0)       # the website is shown this many units less
    orders_since = models.DateTimeField(null=True, blank=True)    # website orders created before this are ignored for good
    orders_cursor = models.DateTimeField(null=True, blank=True)   # the latest order change already seen
    products_cursor = models.DateTimeField(null=True, blank=True)
    running_since = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    last_ok = models.BooleanField(default=True)
    last_message = models.CharField(max_length=300, blank=True)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("The sync settings cannot be deleted.")

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class WooLink(models.Model):
    """Ties a POS product to a website product."""
    product = models.OneToOneField(Product, on_delete=models.CASCADE, related_name="woo_link")
    sell_online = models.BooleanField(default=False)
    woo_product_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    woo_type = models.CharField(max_length=20, blank=True)         # simple, variable ...
    last_pushed_qty = models.IntegerField(null=True, blank=True)
    last_pushed_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    woo_modified = models.DateTimeField(null=True, blank=True)
    sync_status = models.CharField(max_length=20, default="pending")
    sync_error = models.TextField(blank=True)
    needs_review = models.BooleanField(default=False)
    woo_category_ids = models.JSONField(default=list, blank=True)
    woo_image_ids = models.JSONField(default=list, blank=True)
    last_synced_data = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.product} -> {self.woo_product_id}"


class ProductSyncJob(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        FAILED = "failed", "Failed"
        DONE = "done", "Done"
        REVIEW = "review", "Review"

    product = models.ForeignKey(
        Product, null=True, blank=True, on_delete=models.SET_NULL, related_name="sync_jobs"
    )
    woo_product_id = models.PositiveBigIntegerField(null=True, blank=True)
    direction = models.CharField(max_length=20)
    event_type = models.CharField(max_length=30)
    idempotency_key = models.CharField(max_length=100, unique=True)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["status", "available_at"])]


class ProductSyncConflict(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="sync_conflicts")
    field_name = models.CharField(max_length=40)
    pos_value = models.JSONField(null=True, blank=True)
    woo_value = models.JSONField(null=True, blank=True)
    detected_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution = models.CharField(max_length=30, blank=True)

    class Meta:
        ordering = ["-detected_at", "-id"]
class WebOrder(TimeStampedModel):
    """A copy of an order placed on the website."""
    woo_id = models.PositiveBigIntegerField(unique=True)
    number = models.CharField(max_length=40)
    status = models.CharField(max_length=30)
    woo_created = models.DateTimeField(null=True, blank=True)
    woo_modified = models.DateTimeField(null=True, blank=True)
    customer_name = models.CharField(max_length=120, blank=True)
    email = models.CharField(max_length=254, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    billing_address = models.TextField(blank=True)
    shipping_address = models.TextField(blank=True)
    payment_method = models.CharField(max_length=80, blank=True)
    payment_method_code = models.CharField(max_length=80, blank=True)
    transaction_id = models.CharField(max_length=100, blank=True)
    is_paid = models.BooleanField(default=False)
    total = models.DecimalField(default=0, **MONEY)
    currency = models.CharField(max_length=8, blank=True)
    financial_data = models.JSONField(default=dict, blank=True)
    customer_note = models.CharField(max_length=500, blank=True)
    handled = models.BooleanField(default=False)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    handled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-woo_created", "-id"]
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return f"#{self.number}"

    @property
    def holds_stock(self):
        return self.status in HOLDING

    @property
    def badge(self):
        if self.status == "completed":
            return "st-ok"
        if self.status in ("processing", "on-hold"):
            return "st-low"
        if self.status in CLOSED:
            return "st-out"
        return "st-na"


class WebOrderItem(models.Model):
    order = models.ForeignKey(WebOrder, on_delete=models.CASCADE, related_name="items")
    woo_line_id = models.PositiveBigIntegerField()
    woo_product_id = models.PositiveBigIntegerField(null=True, blank=True)
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.PROTECT, related_name="web_items")
    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=100, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))
    line_total = models.DecimalField(default=0, **MONEY)
    applied_qty = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))   # taken from stock so far

    class Meta:
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["order", "woo_line_id"], name="woo_one_row_per_line")]

    def __str__(self):
        return f"{self.name} x {self.quantity}"


class SyncRun(models.Model):
    trigger = models.CharField(max_length=10, default="scheduled")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(default=False)
    orders_seen = models.PositiveIntegerField(default=0)
    orders_new = models.PositiveIntegerField(default=0)
    orders_updated = models.PositiveIntegerField(default=0)
    orders_failed = models.PositiveIntegerField(default=0)
    stock_pushed = models.PositiveIntegerField(default=0)
    summary = models.TextField(blank=True)

    class Meta:
        ordering = ["-id"]