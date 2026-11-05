from decimal import Decimal

from django import forms

from .models import Category, Product


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


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "name", "category", "barcode", "code", "unit", "track_stock",
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
        _style(self)

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