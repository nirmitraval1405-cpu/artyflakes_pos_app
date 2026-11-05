from django import forms

from .models import Contact


class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = [
            "name", "code", "is_customer", "is_supplier", "phone", "email",
            "gstin", "address", "city", "country", "notes", "is_active",
        ]
        help_texts = {
            "code": "Leave blank to auto-generate (CS-00001).",
            "gstin": "Optional. Only needed for business customers or suppliers.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            w = field.widget
            if isinstance(w, forms.CheckboxInput):
                w.attrs["class"] = "form-check-input"
            elif isinstance(w, forms.Select):
                w.attrs["class"] = "form-select"
            else:
                w.attrs["class"] = "form-control"
            if isinstance(w, forms.Textarea):
                w.attrs["rows"] = 2

    def clean_code(self):
        value = self.cleaned_data.get("code")
        return value.strip() or None if value else None

    def clean_gstin(self):
        return (self.cleaned_data.get("gstin") or "").strip().upper()