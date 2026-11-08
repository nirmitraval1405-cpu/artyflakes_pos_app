from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from core.models import TimeStampedModel
from people.models import phone_validator, validate_gstin


def _valid_or_blank(validator, value):
    """A bad first-run value in settings.py must never crash the app. It just starts blank."""
    value = str(value or "").strip()
    try:
        validator(value)
    except ValidationError:
        return ""
    return value


class StoreSettings(TimeStampedModel):
    """The shop's details. Always exactly one row (pk=1)."""
    name = models.CharField(max_length=80, default="Artyflakes")
    tagline = models.CharField(max_length=120, blank=True)
    street_name = models.TextField(blank=True)
    building_number = models.CharField(max_length=30, blank=True)
    additional_street_name = models.CharField(max_length=120, blank=True)
    plot_identification = models.CharField(max_length=120, blank=True)
    district = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state_province = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=60, default="India")
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=15, blank=True, validators=[phone_validator])
    email = models.EmailField(blank=True)
    gstin = models.CharField("GSTIN", max_length=15, blank=True, validators=[validate_gstin])
    bank_account_number = models.CharField(max_length=34, blank=True)
    bank_details = models.TextField(blank=True)
    logo = models.ImageField(upload_to="store/", blank=True)
    receipt_footer = models.CharField(max_length=200, blank=True)
    cashier_max_discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("10"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name_plural = "store settings"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.pk = 1                                   # there is only ever one row
        self.gstin = (self.gstin or "").strip().upper()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Store settings cannot be deleted.")

    @classmethod
    def load(cls):
        obj = cls.objects.filter(pk=1).first()
        if obj is None:
            obj, _ = cls.objects.get_or_create(pk=1, defaults=cls._first_run_defaults())
        return obj

    @staticmethod
    def _first_run_defaults():
        store = getattr(settings, "STORE", None) or {}
        try:
            pct = Decimal(str(getattr(settings, "POS_CASHIER_MAX_DISCOUNT_PERCENT", 10)))
            if not pct.is_finite() or pct < 0 or pct > 100:
                raise InvalidOperation
            pct = pct.quantize(Decimal("0.01"))
        except InvalidOperation:
            pct = Decimal("10.00")
        return {
            "name": (str(store.get("name") or "").strip() or "Artyflakes")[:80],
            "tagline": str(store.get("tagline") or "").strip()[:120],
            "address": str(store.get("address") or "").strip(),
            "phone": _valid_or_blank(phone_validator, store.get("phone"))[:15],
            "gstin": _valid_or_blank(validate_gstin, str(store.get("gstin") or "").upper()),
            "receipt_footer": str(store.get("footer") or "").strip()[:200],
            "cashier_max_discount_percent": pct,
        }

    def as_store_info(self):
        """The shape the receipt and invoice templates use. Also saved on each new bill."""
        address = [
            self.street_name,
            self.building_number,
            self.additional_street_name,
            self.plot_identification,
            self.district,
            self.postal_code,
            self.city,
            self.state_province,
        ]
        if any(part and part.strip() for part in address) and self.country:
            address.append(self.country)
        address = "\n".join(part.strip() for part in address if part and part.strip())
        return {
            "name": self.name, "tagline": self.tagline, "address": address or self.address,
            "phone": self.phone, "email": self.email, "gstin": self.gstin,
            "bank_account_number": self.bank_account_number,
            "bank_details": self.bank_details,
            "logo": self.logo.url if self.logo else "",
            "footer": self.receipt_footer,
        }