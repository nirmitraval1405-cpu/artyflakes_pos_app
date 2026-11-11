from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from core.models import TimeStampedModel


class Category(TimeStampedModel):
    """A product group, shown in the left tree of the Products page."""
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children"
    )
    woo_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"

    def __str__(self):
        names = [self.name]
        ancestor = self.parent
        seen = {self.pk}
        while ancestor and ancestor.pk not in seen:
            names.append(ancestor.name)
            seen.add(ancestor.pk)
            ancestor = ancestor.parent
        return " / ".join(reversed(names))


class Brand(TimeStampedModel):
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Unit(TimeStampedModel):
    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=100, unique=True)
    allow_fraction = models.BooleanField(
        "Allow decimal quantities", default=True,
        help_text="Untick for units that must always be sold or purchased in whole numbers.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"

    @classmethod
    def whole_quantity_codes(cls):
        return set(cls.objects.filter(allow_fraction=False).values_list("code", flat=True))


class Product(TimeStampedModel):
    class Unit(models.TextChoices):
        PCS = "pcs", "Pieces"
        SET = "set", "Set"
        BOX = "box", "Box"
        PACK = "pack", "Pack"
        KG = "kg", "Kilogram"
        G = "g", "Gram"
        LTR = "ltr", "Litre"
        ML = "ml", "Millilitre"
        MTR = "mtr", "Metre"

    code = models.CharField(max_length=30, unique=True, null=True, blank=True)
    name = models.CharField(max_length=200)
    category = models.ForeignKey(
        Category, null=True, blank=True, on_delete=models.SET_NULL, related_name="products"
    )
    brand = models.ForeignKey(
        Brand, null=True, blank=True, on_delete=models.SET_NULL, related_name="products"
    )
    barcode = models.CharField(max_length=64, unique=True, null=True, blank=True)
    hsn_code = models.CharField("HSN code", max_length=10, blank=True)
    description = models.TextField(blank=True)
    short_description = models.TextField(blank=True)
    image = models.ImageField(upload_to="products/", blank=True)
    weight = models.CharField(max_length=30, blank=True)
    dimensions = models.JSONField(default=dict, blank=True)
    attributes = models.JSONField(default=list, blank=True)
    woo_tax_class = models.CharField(max_length=100, blank=True)
    woo_status = models.CharField(max_length=20, blank=True)
    woo_tags = models.JSONField(default=list, blank=True)
    woo_images = models.JSONField(default=list, blank=True)
    unit = models.CharField(max_length=10, default=Unit.PCS)
    track_stock = models.BooleanField(
        "Track stock", default=True,
        help_text="Untick for services or custom work that has no stock.",
    )

    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    sale_price = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    mrp = models.DecimalField("MRP", max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    gst_rate = models.DecimalField(
        "GST %", max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    min_stock = models.PositiveIntegerField("Low stock level", default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.mrp and self.sale_price and self.sale_price > self.mrp:
            raise ValidationError({"sale_price": "Sale price cannot be higher than MRP."})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Auto-generate a code when none was entered, e.g. ART-00001
        if not self.code:
            self.code = f"ART-{self.pk:05d}"
            super().save(update_fields=["code"])


class StockMovement(TimeStampedModel):
    """One row per stock change. Current stock = the sum of a product's rows.
    Rows are never edited or deleted; mistakes are fixed with a new row."""

    class Type(models.TextChoices):
        OPENING = "opening", "Opening stock"
        ADJUSTMENT = "adjustment", "Stock adjustment"
        DAMAGE = "damage", "Damage / loss"
        PURCHASE = "purchase", "Purchase"
        PURCHASE_RETURN = "purchase_return", "Purchase return"
        SALE = "sale", "Sale"
        SALE_RETURN = "sale_return", "Sale return"
        WEB_ORDER = "web_order", "Website order"
        WEB_ORDER_RETURN = "web_order_return", "Website order returned"

    class Source(models.TextChoices):
        POS = "pos", "POS"
        WOOCOMMERCE = "woocommerce", "WooCommerce"

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="movements")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)   # + stock in, - stock out
    movement_type = models.CharField(max_length=20, choices=Type.choices)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.POS)
    reference = models.CharField(max_length=50, blank=True)           # invoice number, later
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["product", "created_at"])]

    def __str__(self):
        return f"{self.product} {self.quantity:+}"


class ProductImage(TimeStampedModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="products/gallery/")
    alt_text = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    woo_media_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)

    class Meta:
        ordering = ["sort_order", "id"]


class ProductTag(TimeStampedModel):
    name = models.CharField(max_length=100, unique=True)
    woo_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    description = models.TextField(blank=True)
    products = models.ManyToManyField(Product, related_name="tags", blank=True)

    class Meta:
        ordering = ["name"]