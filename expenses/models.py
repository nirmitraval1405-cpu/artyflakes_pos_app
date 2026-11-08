from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel


class ExpenseCategory(TimeStampedModel):
    name = models.CharField(max_length=80, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "expense categories"

    def __str__(self):
        return self.name


class ExpenseCounter(models.Model):
    year = models.PositiveIntegerField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    @classmethod
    def next_expense_no(cls):
        year = timezone.localdate().year
        counter, _ = cls.objects.select_for_update().get_or_create(year=year)
        counter.last_number += 1
        counter.save(update_fields=["last_number"])
        return f"AF-E-{year}-{counter.last_number:04d}"


class Expense(TimeStampedModel):
    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Cash"
        UPI = "upi", "UPI"
        CARD = "card", "Card"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        VOIDED = "voided", "Voided"

    expense_no = models.CharField(max_length=40, unique=True)
    expense_date = models.DateField(default=timezone.localdate)
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name="expenses")
    amount = models.DecimalField(
        max_digits=14, decimal_places=2, validators=[MinValueValidator(0.01)]
    )
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    payee = models.CharField(max_length=120, blank=True)
    reference = models.CharField(max_length=80, blank=True)
    note = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="voided_expenses",
    )
    void_reason = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_expenses"
    )
    client_token = models.CharField(max_length=40, unique=True, null=True, blank=True)

    class Meta:
        ordering = ["-expense_date", "-id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="expense_amount_positive"),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="active",
                        voided_at__isnull=True,
                        voided_by__isnull=True,
                        void_reason="",
                    )
                    | (
                        models.Q(status="voided", voided_at__isnull=False, voided_by__isnull=False)
                        & ~models.Q(void_reason="")
                    )
                ),
                name="expense_void_fields_match_status",
            ),
        ]

    def __str__(self):
        return self.expense_no

    def clean(self):
        super().clean()
        if self.expense_date and self.expense_date > timezone.localdate():
            raise ValidationError({"expense_date": "Expense date cannot be in the future."})
