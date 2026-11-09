from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import TimeStampedModel
from inventory.models import Brand, Category, Product

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class Promotion(TimeStampedModel):
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    weekdays = models.CharField(max_length=13, blank=True, default="")    # "0,2,4" = Mon, Wed, Fri. Blank = every day
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-is_active", "-id"]

    def __str__(self):
        return self.name

    def weekday_set(self):
        return {int(x) for x in self.weekdays.split(",") if x.strip().isdigit() and 0 <= int(x) <= 6}

    @property
    def days_label(self):
        days = sorted(self.weekday_set())
        return "Every day" if not days else ", ".join(DAY_NAMES[d][:3] for d in days)

    def state(self, now=None):
        """(key, label) for the list page."""
        now = now or timezone.now()
        if not self.is_active:
            return "off", "Off"
        if self.starts_at and self.starts_at > now:
            return "scheduled", "Scheduled"
        if self.ends_at and self.ends_at < now:
            return "expired", "Expired"
        days = self.weekday_set()
        if days and timezone.localtime(now).weekday() not in days:
            return "idle", "Active (not today)"
        return "live", "Live now"


class PromotionItem(models.Model):
    class Kind(models.TextChoices):
        PERCENT = "percent", "Percent (%) off"
        AMOUNT = "amount", "Amount (\u20b9) off each"

    promotion = models.ForeignKey(Promotion, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.CASCADE, related_name="promotion_items")
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.CASCADE, related_name="promotion_items")
    brand = models.ForeignKey(Brand, null=True, blank=True, on_delete=models.CASCADE, related_name="promotion_items")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PERCENT)
    value = models.DecimalField(max_digits=10, decimal_places=2)
    min_qty = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)   # blank = no condition

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(product__isnull=False, category__isnull=True, brand__isnull=True)
                    | Q(product__isnull=True, category__isnull=False, brand__isnull=True)
                    | Q(product__isnull=True, category__isnull=True, brand__isnull=False)
                ),
                name="promotion_item_exactly_one_target",
            ),
        ]

    def __str__(self):
        target = self.product or self.category or self.brand
        return f"{target} ({self.value})"

    @property
    def value_label(self):
        text = f"{self.value.normalize():f}%" if self.kind == self.Kind.PERCENT else f"\u20b9{self.value:,.2f} each"
        if self.min_qty:
            text += f" (buy {self.min_qty.normalize():f}+)"
        return text