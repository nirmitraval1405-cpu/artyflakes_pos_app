from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel
from inventory.models import Product
from people.models import Contact

MONEY = {"max_digits": 12, "decimal_places": 2}


class InvoiceCounter(models.Model):
    """One row per year. Locked while a number is taken, so two counters never get the same invoice number."""
    year = models.PositiveIntegerField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    @classmethod
    def next_invoice_no(cls):
        """Must be called inside transaction.atomic()."""
        year = timezone.localdate().year
        counter, _ = cls.objects.select_for_update().get_or_create(year=year)
        counter.last_number += 1
        counter.save(update_fields=["last_number"])
        return f"AF-{year}-{counter.last_number:04d}"


class ReturnCounter(models.Model):
    """Same idea as InvoiceCounter, for return numbers like AF-R-2026-0001."""
    year = models.PositiveIntegerField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    @classmethod
    def next_return_no(cls):
        """Must be called inside transaction.atomic()."""
        year = timezone.localdate().year
        counter, _ = cls.objects.select_for_update().get_or_create(year=year)
        counter.last_number += 1
        counter.save(update_fields=["last_number"])
        return f"AF-R-{year}-{counter.last_number:04d}"


class Sale(TimeStampedModel):
    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        VOIDED = "voided", "Voided"

    invoice_no = models.CharField(max_length=30, unique=True)
    client_token = models.CharField(max_length=40, unique=True, null=True, blank=True)  # blocks double submits
    customer = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="sales")
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.COMPLETED)

    # Prices include GST. subtotal = sum of price x qty, before any discount.
    subtotal = models.DecimalField(default=0, **MONEY)
    bill_discount = models.DecimalField(default=0, **MONEY)   # the part entered at bill level
    discount_total = models.DecimalField(default=0, **MONEY)  # line discounts + bill discount
    tax_total = models.DecimalField(default=0, **MONEY)       # GST contained in the total
    total = models.DecimalField(default=0, **MONEY)           # what the customer pays
    amount_paid = models.DecimalField(default=0, **MONEY)
    change_due = models.DecimalField(default=0, **MONEY)
    note = models.CharField(max_length=255, blank=True)
    store_info = models.JSONField(null=True, blank=True)

    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    void_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["created_at"])]

    def __str__(self):
        return self.invoice_no


class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="sale_items")

    # Copied from the product at the moment of sale, so later edits never change old bills
    product_name = models.CharField(max_length=200)
    product_code = models.CharField(max_length=30, blank=True)
    hsn_code = models.CharField(max_length=10, blank=True)
    unit = models.CharField(max_length=10, blank=True)

    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_price = models.DecimalField(default=0, **MONEY)   # includes GST
    mrp = models.DecimalField(default=0, **MONEY)
    cost_price = models.DecimalField(default=0, **MONEY)   # for profit reports later
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    discount = models.DecimalField(default=0, **MONEY)     # total discount on this line, incl. share of bill discount
    tax_amount = models.DecimalField(default=0, **MONEY)   # GST contained in line_total
    line_total = models.DecimalField(default=0, **MONEY)   # unit_price x quantity - discount

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"


class Payment(TimeStampedModel):
    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        UPI = "upi", "UPI"
        CARD = "card", "Card"
        CHEQUE = "cheque", "Cheque"

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="payments")
    method = models.CharField(max_length=10, choices=Method.choices)
    amount = models.DecimalField(**MONEY)
    reference = models.CharField(max_length=60, blank=True)   # UPI / card transaction id

    def __str__(self):
        return f"{self.get_method_display()} {self.amount}"


class SaleReturn(TimeStampedModel):
    """Money handed back for items returned from one bill."""
    return_no = models.CharField(max_length=30, unique=True)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="returns")
    processed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    refund_method = models.CharField(max_length=10, choices=Payment.Method.choices)
    refund_total = models.DecimalField(default=0, **MONEY)
    tax_total = models.DecimalField(default=0, **MONEY)       # GST contained in the refund
    reason = models.CharField(max_length=255, blank=True)
    client_token = models.CharField(max_length=40, unique=True, null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.return_no


class SaleReturnItem(models.Model):
    sale_return = models.ForeignKey(SaleReturn, on_delete=models.CASCADE, related_name="items")
    sale_item = models.ForeignKey(SaleItem, on_delete=models.PROTECT, related_name="returned_items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    product_name = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    refund_amount = models.DecimalField(default=0, **MONEY)
    tax_amount = models.DecimalField(default=0, **MONEY)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"
    
class HeldSale(TimeStampedModel):
    """A bill saved with F9 to finish later. Not a real sale: no invoice number, no stock change."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    customer = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    data = models.JSONField(default=dict)
    item_count = models.PositiveIntegerField(default=0)
    total = models.DecimalField(default=0, **MONEY)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Held sale #{self.pk}"


class DrawerEvent(TimeStampedModel):
    """Every time someone opens the cash drawer from the POS."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=120)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.reason} ({self.created_at:%d %b %H:%M})"