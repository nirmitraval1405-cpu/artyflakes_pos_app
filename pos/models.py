import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from core.models import TimeStampedModel


def new_order_number():
    return f"POS-{uuid.uuid4().hex[:12].upper()}"


class Sale(TimeStampedModel):
    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        VOID = "void", "Void"

    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        UPI = "upi", "UPI"
        CHECK = "check", "Check"
        SPLIT = "split", "Split"

    order_number = models.CharField(max_length=20, unique=True, default=new_order_number)
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="pos_sales"
    )
    customer = models.ForeignKey(
        "people.Contact", null=True, blank=True, on_delete=models.SET_NULL, related_name="sales"
    )
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    tax = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    discount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    total = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    payment_method = models.CharField(max_length=10, choices=PaymentMethod.choices)
    payment_details = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.COMPLETED)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.order_number


class SaleItem(TimeStampedModel):
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="items")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT, related_name="sale_items")
    quantity = models.DecimalField(
        max_digits=12, decimal_places=3, validators=[MinValueValidator(0.001)]
    )
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    discount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="pos_sale_item_qty_positive"),
        ]

    def __str__(self):
        return f"{self.sale.order_number}: {self.product}"


class HeldSale(TimeStampedModel):
    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="held_pos_sales"
    )
    cart = models.JSONField()
    customer_name = models.CharField(max_length=200, blank=True)
    customer_phone = models.CharField(max_length=15, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Held sale {self.pk} by {self.cashier}"
