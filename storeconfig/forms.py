from django import forms

from .models import StoreSettings


class StoreSettingsForm(forms.ModelForm):
    class Meta:
        model = StoreSettings
        fields = [
            "name", "gstin", "street_name", "building_number", "additional_street_name",
            "plot_identification", "district", "postal_code", "city", "state_province",
            "country", "phone", "email", "bank_account_number", "bank_details", "logo",
            "tagline", "receipt_footer", "cashier_max_discount_percent",
        ]
        labels = {
            "name": "Name",
            "gstin": "Tax number",
            "street_name": "Street name",
            "building_number": "Building number",
            "additional_street_name": "Additional street name",
            "plot_identification": "Plot identification",
            "postal_code": "Postal code",
            "state_province": "State / Province",
            "bank_account_number": "Bank account number",
            "bank_details": "Bank details",
            "logo": "Logo",
            "receipt_footer": "Receipt footer",
            "cashier_max_discount_percent": "Cashier discount limit (%)",
        }
        help_texts = {
            "name": "Shown on receipts and invoices.",
            "phone": "Digits only, 7 to 15 characters.",
            "gstin": "Leave blank if the shop is not GST registered. When filled, bills are titled TAX INVOICE.",
            "logo": "Optional image shown on receipts and invoices. Maximum file size: 2 MB.",
            "receipt_footer": "A short thank-you line at the bottom of every bill.",
            "cashier_max_discount_percent": "The most a cashier may discount a bill, as a percent of the bill. Admins have no limit. Applies from the next bill.",
        }
        widgets = {
            "street_name": forms.Textarea(attrs={"rows": 2}),
            "bank_details": forms.Textarea(attrs={"rows": 3}),
            "country": forms.TextInput(attrs={"list": "store-country-options"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean_name(self):
        return (self.cleaned_data.get("name") or "").strip()

    def clean_gstin(self):
        return (self.cleaned_data.get("gstin") or "").strip().upper()

    def clean_logo(self):
        logo = self.cleaned_data.get("logo")
        if logo and logo.size > 2 * 1024 * 1024:
            raise forms.ValidationError("Logo file must be 2 MB or smaller.")
        return logo