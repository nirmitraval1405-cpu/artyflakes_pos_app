from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

from inventory.models import Product
from people.models import Contact


class PurchaseHeaderForm(forms.Form):
    supplier = forms.ModelChoiceField(
        queryset=Contact.objects.filter(is_supplier=True, is_active=True).order_by("name"),
        label="Supplier",
        empty_label="Select supplier",
    )
    supplier_invoice_no = forms.CharField(max_length=80, required=False, label="Supplier invoice no.")
    purchase_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), label="Purchase date")
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Purchase notes"}), required=False)
    amount_paid = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0"),
        required=False,
        initial=Decimal("0.00"),
        label="Amount paid now",
    )
    payment_due = forms.BooleanField(
        required=False,
        initial=False,
        label="Payment due",
    )
    payment_method = forms.ChoiceField(
        choices=[
            ("cash", "Cash"),
            ("upi", "UPI"),
            ("card", "Card"),
            ("bank_transfer", "Bank transfer"),
        ],
        required=False,
        initial="cash",
        label="Payment method",
    )
    payment_reference = forms.CharField(max_length=80, required=False, label="Payment reference")

    def clean(self):
        cleaned = super().clean()
        paid = cleaned.get("amount_paid") or Decimal("0")
        if paid < 0:
            raise ValidationError("Payment amount cannot be negative.")
        return cleaned


class PurchaseItemForm(forms.Form):
    product = forms.ModelChoiceField(
        queryset=Product.objects.filter(is_active=True).order_by("name"),
        label="Product",
        empty_label="Select product",
    )
    quantity = forms.DecimalField(min_value=Decimal("0.001"), max_digits=12, decimal_places=3, label="Qty")
    cost_price = forms.DecimalField(min_value=Decimal("0"), max_digits=12, decimal_places=2, label="Cost price")
    discount = forms.DecimalField(min_value=Decimal("0"), max_digits=12, decimal_places=2, initial=Decimal("0.00"), label="Discount")
    gst_rate = forms.DecimalField(min_value=Decimal("0"), max_digits=5, decimal_places=2, initial=Decimal("0.00"), label="GST %")
