from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel
from inventory.models import Product, StockMovement
from people.models import Contact

MONEY = {"max_digits": 14, "decimal_places": 2}
ZERO = Decimal("0")


class PurchaseCounter(models.Model):
    year = models.PositiveIntegerField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    @classmethod
    def next_purchase_no(cls):
        year = timezone.localdate().year
        counter, _ = cls.objects.select_for_update().get_or_create(year=year)
        counter.last_number += 1
        counter.save(update_fields=["last_number"])
        return f"AF-P-{year}-{counter.last_number:04d}"


class Purchase(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    client_token = models.CharField(max_length=40, unique=True, null=True, blank=True)  # blocks double submits
    purchase_no = models.CharField(max_length=40, unique=True)
    supplier = models.ForeignKey(
        Contact,
        on_delete=models.PROTECT,
        related_name="purchases",
        limit_choices_to={"is_supplier": True},
    )
    supplier_invoice_no = models.CharField(max_length=80, blank=True)
    purchase_date = models.DateField(default=timezone.localdate)
    subtotal = models.DecimalField(default=0, **MONEY)
    discount_total = models.DecimalField(default=0, **MONEY)
    taxable_amount = models.DecimalField(default=0, **MONEY)
    cgst = models.DecimalField(default=0, **MONEY)
    sgst = models.DecimalField(default=0, **MONEY)
    igst = models.DecimalField(default=0, **MONEY)
    grand_total = models.DecimalField(default=0, **MONEY)
    amount_paid = models.DecimalField(default=0, **MONEY)
    outstanding_amount = models.DecimalField(default=0, **MONEY)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    notes = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_purchases",
    )
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cancelled_purchases",
    )
    cancel_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-purchase_date", "-id"]
        indexes = [models.Index(fields=["supplier", "purchase_date"])]

    def __str__(self):
        return self.purchase_no

    @property
    def payment_status(self):
        if self.outstanding_amount <= ZERO:
            return "Paid"
        if self.amount_paid > ZERO:
            return "Partial"
        return "Unpaid"

    def update_balances(self):
        self.amount_paid = max(ZERO, min(self.amount_paid, self.grand_total))
        self.outstanding_amount = max(ZERO, self.grand_total - self.amount_paid)

    def clean(self):
        super().clean()
        if self.grand_total < ZERO:
            raise ValidationError("Grand total cannot be negative.")
        if self.amount_paid < ZERO:
            raise ValidationError("Amount paid cannot be negative.")

    def save(self, *args, **kwargs):
        self.outstanding_amount = max(ZERO, self.grand_total - self.amount_paid)
        super().save(*args, **kwargs)


class PurchaseItem(models.Model):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="purchase_items")
    product_name = models.CharField(max_length=200)
    product_code = models.CharField(max_length=30, blank=True)
    hsn_code = models.CharField(max_length=10, blank=True)
    unit = models.CharField(max_length=10, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    cost_price = models.DecimalField(default=0, **MONEY)
    discount = models.DecimalField(default=0, **MONEY)
    gst_rate = models.DecimalField(default=0, max_digits=5, decimal_places=2)
    taxable_amount = models.DecimalField(default=0, **MONEY)
    gst_amount = models.DecimalField(default=0, **MONEY)
    total_amount = models.DecimalField(default=0, **MONEY)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"


class PurchasePayment(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        UPI = "upi", "UPI"
        CARD = "card", "Card"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"

    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="payments")
    method = models.CharField(max_length=20, choices=Method.choices)
    amount = models.DecimalField(default=0, **MONEY)
    payment_date = models.DateField(default=timezone.localdate)
    reference = models.CharField(max_length=80, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_purchase_payments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-payment_date", "-id"]

    def __str__(self):
        return f"{self.purchase.purchase_no} - {self.get_method_display()}"


def create_stock_entry_for_purchase(purchase, product, quantity, user):
    if not product.track_stock:
        return None
    return StockMovement.objects.create(
        product=product,
        quantity=quantity,
        movement_type=StockMovement.Type.PURCHASE,
        reference=purchase.purchase_no,
        note=f"Purchase {purchase.purchase_no}",
        created_by=user,
    )
