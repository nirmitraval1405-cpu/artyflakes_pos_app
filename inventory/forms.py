import re
from decimal import Decimal

from django import forms
from django.db.models import Q

from .models import Brand, Category, Product, Unit


def _style(form):
    for field in form.fields.values():
        w = field.widget
        if isinstance(w, forms.CheckboxInput):
            w.attrs["class"] = "form-check-input"
        elif isinstance(w, forms.Select):
            w.attrs["class"] = "form-select"
        else:
            w.attrs["class"] = "form-control"


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


class BrandForm(forms.ModelForm):
    class Meta:
        model = Brand
        fields = ["name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


class UnitForm(forms.ModelForm):
    class Meta:
        model = Unit
        fields = ["code", "name", "allow_fraction"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["code"].disabled = True
        _style(self)

    def clean_code(self):
        code = self.cleaned_data["code"].strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]+", code):
            raise forms.ValidationError("Use lowercase letters, numbers, hyphens, or underscores.")
        return code


class ProductForm(forms.ModelForm):
    opening_stock = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        max_digits=12,
        decimal_places=3,
        initial=Decimal("0"),
        label="Opening stock",
        help_text="Optional. Set the current quantity when creating this product.",
    )

    class Meta:
        model = Product
        fields = [
            "name", "category", "brand", "barcode", "code", "unit", "track_stock",
            "cost_price", "sale_price", "mrp", "gst_rate", "hsn_code",
            "min_stock", "is_active",
        ]
        help_texts = {
            "code": "Leave blank to auto-generate (ART-00001).",
            "barcode": "Optional. Scan or type it. Must be unique.",
            "mrp": "Maximum retail price. Sale price cannot be higher.",
            "min_stock": "Shows in Low Stock when stock reaches this number.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(
            Q(is_active=True) | Q(pk=self.instance.category_id)
        ).order_by("name")
        self.fields["brand"].queryset = Brand.objects.filter(
            Q(is_active=True) | Q(pk=self.instance.brand_id)
        ).order_by("name")
        active_units = Unit.objects.filter(is_active=True)
        if self.instance.pk:
            active_units = Unit.objects.filter(
                Q(is_active=True) | Q(code=self.instance.unit)
            )
        self.fields["unit"] = forms.ChoiceField(
            choices=[(unit.code, f"{unit.name} ({unit.code})") for unit in active_units],
            initial=self.instance.unit or Product.Unit.PCS,
            label="Unit",
        )
        if self.instance.pk:
            self.fields.pop("opening_stock")
        else:
            self.order_fields([
                "name", "category", "brand", "barcode", "code", "unit", "track_stock",
                "opening_stock", "cost_price", "sale_price", "mrp", "gst_rate",
                "hsn_code", "min_stock", "is_active",
            ])
        _style(self)

    def clean(self):
        cleaned_data = super().clean()
        opening_stock = cleaned_data.get("opening_stock") or Decimal("0")
        if (
            not cleaned_data.get("track_stock")
            and opening_stock > 0
        ):
            self.add_error("opening_stock", "Opening stock requires stock tracking to be enabled.")
        return cleaned_data

    def clean_barcode(self):
        value = self.cleaned_data.get("barcode")
        return value.strip() or None if value else None

    def clean_code(self):
        value = self.cleaned_data.get("code")
        return value.strip() or None if value else None


# ---------- Stock entry forms ----------
class ProductChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.name} ({obj.code})"


class _StockForm(forms.Form):
    product = ProductChoice(
        queryset=Product.objects.filter(track_stock=True, is_active=True).order_by("name")
    )
    note = forms.CharField(required=False, max_length=255)
    field_order = ["product", "quantity", "counted", "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)


class AddStockForm(_StockForm):
    quantity = forms.DecimalField(
        min_value=Decimal("0.001"), max_digits=12, decimal_places=3,
        label="Quantity to add",
        help_text="The first entry for a product is recorded as Opening stock.",
    )


class CountForm(_StockForm):
    counted = forms.DecimalField(
        min_value=Decimal("0"), max_digits=12, decimal_places=3,
        label="Counted quantity",
        help_text="What you physically counted. The system records the difference.",
    )


class DamageForm(_StockForm):
    quantity = forms.DecimalField(
        min_value=Decimal("0.001"), max_digits=12, decimal_places=3,
        label="Quantity damaged or lost",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["note"].required = True
        self.fields["note"].label = "Reason"