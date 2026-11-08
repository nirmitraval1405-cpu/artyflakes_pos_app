from decimal import Decimal

from django import forms

from .models import Expense, ExpenseCategory
from .services import MAX_AMOUNT


class ExpenseForm(forms.Form):
    expense_date = forms.DateField(
        label="Expense date",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    category = forms.ModelChoiceField(
        label="Category",
        queryset=ExpenseCategory.objects.none(),
        empty_label="Select category",
    )
    amount = forms.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
        max_value=MAX_AMOUNT,
        label="Amount (₹)",
    )
    payment_method = forms.ChoiceField(
        label="Payment method",
        choices=Expense.PaymentMethod.choices,
    )
    payee = forms.CharField(max_length=120, required=False, label="Payee")
    reference = forms.CharField(max_length=80, required=False, label="Reference")
    note = forms.CharField(max_length=255, required=False, label="Note")
    client_token = forms.CharField(max_length=40, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = ExpenseCategory.objects.filter(is_active=True).order_by("name")


class ExpenseCategoryForm(forms.Form):
    name = forms.CharField(max_length=80, label="Category name")


class ExpenseVoidForm(forms.Form):
    reason = forms.CharField(
        max_length=255,
        label="Reason for voiding",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
