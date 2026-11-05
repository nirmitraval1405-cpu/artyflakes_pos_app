from django import forms

from .utils.barcode_generator import validate_barcode
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
            "name", "category", "barcode", "code", "unit",
            "cost_price", "sale_price", "mrp", "gst_rate", "hsn_code",
            "min_stock", "is_active",
        ]
        help_texts = {
            "code": "Leave blank to auto-generate (ART-00001).",
            "barcode": "Optional. Scan or type it. Must be unique. Generated values use CODE128, not EAN-13.",
            "mrp": "Maximum retail price. Sale price cannot be higher.",
            "min_stock": "Used later for low-stock warnings.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)

    def clean_barcode(self):
        value = self.cleaned_data.get("barcode")
        if not value:
            return None
        value = value.strip()
        if not value:
            return None
        return validate_barcode(value, exclude_pk=self.instance.pk)

    def clean_code(self):
        value = self.cleaned_data.get("code")
        return value.strip() or None if value else None