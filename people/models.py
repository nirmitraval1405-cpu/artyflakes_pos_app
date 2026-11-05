import re

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from core.models import TimeStampedModel

GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$", re.IGNORECASE)

phone_validator = RegexValidator(
    r"^[0-9+\-\s]{7,15}$", "Enter a valid phone number (digits, spaces, + or - only)."
)


def validate_gstin(value):
    if value and not GSTIN_RE.match(value.strip()):
        raise ValidationError("Enter a valid 15-character GSTIN, for example 24ABCDE1234F1Z5.")


class Contact(TimeStampedModel):
    """A customer, a supplier, or both."""
    code = models.CharField(max_length=30, unique=True, null=True, blank=True)
    name = models.CharField(max_length=200)
    is_customer = models.BooleanField("Customer", default=True)
    is_supplier = models.BooleanField("Supplier", default=False)

    gstin = models.CharField("Tax number (GSTIN)", max_length=15, blank=True, validators=[validate_gstin])
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=60, default="India", blank=True)
    phone = models.CharField("Phone number", max_length=15, blank=True, db_index=True, validators=[phone_validator])
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)

    is_active = models.BooleanField(default=True)
    is_system = models.BooleanField(default=False, editable=False)  # the built-in Walk-in customer

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def type_label(self):
        if self.is_customer and self.is_supplier:
            return "Both"
        return "Supplier" if self.is_supplier else "Customer"

    def clean(self):
        super().clean()
        if not (self.is_customer or self.is_supplier):
            raise ValidationError("Tick Customer, Supplier, or both.")

    def save(self, *args, **kwargs):
        self.gstin = (self.gstin or "").strip().upper()
        super().save(*args, **kwargs)
        if not self.code and not self.is_system:
            self.code = f"CS-{self.pk:05d}"
            super().save(update_fields=["code"])

    @classmethod
    def walk_in(cls):
        """The built-in customer used for bills with no named customer."""
        obj, _ = cls.objects.get_or_create(
            is_system=True,
            defaults={"name": "Walk-in customer", "is_customer": True, "is_supplier": False},
        )
        return obj