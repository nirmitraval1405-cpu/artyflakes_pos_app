import re
from decimal import Decimal

from django import forms
from django.db.models import Q

from .models import Brand, Category, Product, ProductImage, Unit


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
        fields = ["name", "description", "parent"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].label = "Parent category"
        self.fields["parent"].empty_label = "Main category (no parent)"
        self.fields["parent"].help_text = (
            "Leave this empty for a main category, or select a parent to create a subcategory."
        )
        self.fields["parent"].queryset = Category.objects.select_related("parent").order_by("parent__name", "name")
        _style(self)

    def clean_parent(self):
        parent = self.cleaned_data.get("parent")
        ancestor = parent
        seen = set()
        while ancestor:
            if ancestor.pk == self.instance.pk or ancestor.pk in seen:
                raise forms.ValidationError("A category cannot be its own parent or a descendant of itself.")
            seen.add(ancestor.pk)
            ancestor = ancestor.parent
        return parent


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, value, initial=None):
        if not value:
            return []
        if isinstance(value, (list, tuple)):
            return [forms.FileField.clean(self, item, None) for item in value]
        return [super().clean(value, initial)]


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
    gallery_images = MultipleFileField(
        required=False,
        label="Additional product images",
        help_text="Select one or more images to add to the WooCommerce gallery.",
    )

    tags_text = forms.CharField(
        required=False,
        label="Website tags",
        help_text="Comma-separated WooCommerce product tags.",
    )

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
            "name", "description", "short_description", "image", "category", "brand", "barcode", "code", "unit", "track_stock",
            "cost_price", "sale_price", "mrp", "gst_rate", "hsn_code",
            "woo_tax_class", "weight", "dimensions", "attributes", "min_stock", "is_active",
        ]
        help_texts = {
            "code": "Leave blank to auto-generate (ART-00001).",
            "barcode": "Optional. Scan or type it. Must be unique.",
            "mrp": "Maximum retail price. Sale price cannot be higher.",
            "min_stock": "Shows in Low Stock when stock reaches this number.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["dimensions"] = forms.CharField(
            required=False,
            label="Dimensions (length × width × height)",
            help_text="Enter dimensions separated by × or x.",
            initial=" × ".join(str(self.instance.dimensions.get(key, "")) for key in ("length", "width", "height"))
            if self.instance.pk and isinstance(self.instance.dimensions, dict) else "",
        )
        self.fields["tags_text"].initial = ", ".join(
            tag.get("name", "") for tag in self.instance.woo_tags if isinstance(tag, dict) and tag.get("name")
        ) if self.instance.pk and isinstance(self.instance.woo_tags, list) else ""
        if self.instance.pk:
            self.fields["gallery_images"].help_text = (
                "Select more images to add. Existing images are shown below."
            )
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
        self.order_fields([
            "name", "description", "short_description", "image", "gallery_images",
            "category", "brand", "barcode", "code", "unit", "track_stock",
            *([] if self.instance.pk else ["opening_stock"]),
            "cost_price", "sale_price", "mrp", "gst_rate", "hsn_code", "woo_tax_class",
            "weight", "dimensions", "attributes", "tags_text", "min_stock", "is_active",
        ])
        _style(self)

    def clean_dimensions(self):
        value = self.cleaned_data.get("dimensions", "")
        parts = [part.strip() for part in value.replace("×", "x").split("x")]
        if not value.strip():
            return {}
        if len(parts) != 3:
            raise forms.ValidationError("Enter length, width, and height separated by x.")
        return dict(zip(("length", "width", "height"), parts))

    def clean_tags_text(self):
        return [
            {"name": name.strip()}
            for name in self.cleaned_data.get("tags_text", "").split(",")
            if name.strip()
        ]

    def save(self, commit=True):
        product = super().save(commit=False)
        product.dimensions = self.cleaned_data.get("dimensions", {})
        product.woo_tags = self.cleaned_data.get("tags_text", [])
        if commit:
            product.save()
            self.save_m2m()
            next_order = product.images.count()
            for offset, image in enumerate(self.cleaned_data.get("gallery_images", [])):
                ProductImage.objects.create(
                    product=product,
                    image=image,
                    sort_order=next_order + offset,
                )
        return product

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