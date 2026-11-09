from django import forms

from .models import DAY_NAMES, Promotion

DT_FORMAT = "%Y-%m-%dT%H:%M"


class PromotionForm(forms.ModelForm):
    all_days = forms.BooleanField(required=False, initial=True, label="All days")
    days = forms.MultipleChoiceField(
        required=False,
        choices=[(str(i), name) for i, name in enumerate(DAY_NAMES)],
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input day-box"}),
    )
    starts_at = forms.DateTimeField(
        required=False, label="Start", input_formats=[DT_FORMAT],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}, format=DT_FORMAT),
    )
    ends_at = forms.DateTimeField(
        required=False, label="End", input_formats=[DT_FORMAT],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}, format=DT_FORMAT),
    )

    class Meta:
        model = Promotion
        fields = ["name", "is_active", "starts_at", "ends_at"]
        labels = {"name": "Promotion name", "is_active": "Active"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs["class"] = "form-control"
        self.fields["is_active"].widget.attrs["class"] = "form-check-input"
        self.fields["all_days"].widget.attrs["class"] = "form-check-input"
        if self.instance and self.instance.pk:
            days = self.instance.weekday_set()
            self.initial["all_days"] = not days
            self.initial["days"] = [str(d) for d in sorted(days)]

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Give the promotion a name.")
        return name

    def clean(self):
        cd = super().clean()
        start, end = cd.get("starts_at"), cd.get("ends_at")
        if start and end and end <= start:
            self.add_error("ends_at", "The end must be after the start.")
        if not cd.get("all_days") and not cd.get("days"):
            self.add_error("days", "Pick at least one day, or tick All days.")
        return cd

    def weekdays_value(self):
        if self.cleaned_data.get("all_days"):
            return ""
        chosen = sorted(set(self.cleaned_data.get("days") or []), key=int)
        return "" if len(chosen) == 7 else ",".join(chosen)