from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Expense, ExpenseCategory, ExpenseCounter

MAX_AMOUNT = Decimal("100000000")
TWO_PLACES = Decimal("0.01")
DEFAULT_CATEGORIES = (
    "Rent",
    "Salary",
    "Electricity",
    "Transport",
    "Packaging",
    "Marketing",
    "Repairs",
    "Miscellaneous",
)


class ExpenseError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def ensure_default_categories():
    with transaction.atomic():
        existing = set(
            ExpenseCategory.objects.filter(name__in=DEFAULT_CATEGORIES).values_list("name", flat=True)
        )
        for name in DEFAULT_CATEGORIES:
            if name not in existing:
                ExpenseCategory.objects.get_or_create(name=name, defaults={"is_active": True})


def validate_amount(value):
    if value is None or isinstance(value, bool):
        raise ExpenseError("Amount is required.")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise ExpenseError("Amount must be a valid number.")
    if not amount.is_finite():
        raise ExpenseError("Amount must be a valid number.")
    if amount <= 0 or amount > MAX_AMOUNT:
        raise ExpenseError("Amount must be above zero and no more than ₹10,00,00,000.")
    if amount.as_tuple().exponent < -2:
        raise ExpenseError("Amount can have at most 2 decimal places.")
    return amount.quantize(TWO_PLACES)


def parse_expense_date(value):
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        expense_date = value
    else:
        try:
            expense_date = date.fromisoformat(str(value or "").strip())
        except (TypeError, ValueError):
            raise ExpenseError("Expense date is not a valid date.")
    if expense_date > timezone.localdate():
        raise ExpenseError("Expense date cannot be in the future.")
    return expense_date


def _category_id(value):
    if isinstance(value, ExpenseCategory):
        return value.pk
    if isinstance(value, bool):
        raise ExpenseError("Select a valid expense category.")
    try:
        category_id = int(value)
    except (TypeError, ValueError):
        raise ExpenseError("Select a valid expense category.")
    if category_id <= 0:
        raise ExpenseError("Select a valid expense category.")
    return category_id


def _text(value, label, limit):
    text = str(value or "").strip()
    if len(text) > limit:
        raise ExpenseError(f"{label} can be at most {limit} characters.")
    return text


def create_expense(user, data):
    token = str(data.get("client_token") or "")[:40] or None
    if token:
        existing = Expense.objects.filter(client_token=token).first()
        if existing:
            return existing, True

    amount = validate_amount(data.get("amount"))
    expense_date = parse_expense_date(data.get("expense_date"))
    method = data.get("payment_method")
    if method not in Expense.PaymentMethod.values:
        raise ExpenseError("Select a valid payment method.")
    category_id = _category_id(data.get("category"))
    payee = _text(data.get("payee"), "Payee", 120)
    reference = _text(data.get("reference"), "Reference", 80)
    note = _text(data.get("note"), "Note", 255)

    try:
        with transaction.atomic():
            category = ExpenseCategory.objects.select_for_update().filter(
                pk=category_id, is_active=True
            ).first()
            if category is None:
                raise ExpenseError("That expense category is no longer active.")
            expense = Expense.objects.create(
                expense_no=ExpenseCounter.next_expense_no(),
                expense_date=expense_date,
                category=category,
                amount=amount,
                payment_method=method,
                payee=payee,
                reference=reference,
                note=note,
                created_by=user,
                client_token=token,
            )
            return expense, False
    except IntegrityError:
        if token:
            existing = Expense.objects.filter(client_token=token).first()
            if existing:
                return existing, True
        raise


def void_expense(user, expense_id, reason):
    reason = str(reason or "").strip()
    if not reason:
        raise ExpenseError("A reason is required to void an expense.")
    if len(reason) > 255:
        raise ExpenseError("Void reason can be at most 255 characters.")

    with transaction.atomic():
        expense = Expense.objects.select_for_update().filter(pk=expense_id).first()
        if expense is None:
            raise ExpenseError("Expense not found.", status=404)
        if expense.status != Expense.Status.ACTIVE:
            raise ExpenseError("This expense is already voided.")
        expense.status = Expense.Status.VOIDED
        expense.voided_at = timezone.now()
        expense.voided_by = user
        expense.void_reason = reason
        expense.save(update_fields=["status", "voided_at", "voided_by", "void_reason", "updated_at"])
    return expense


def create_category(name):
    name = str(name or "").strip()
    if not name:
        raise ExpenseError("Category name is required.")
    if len(name) > 80:
        raise ExpenseError("Category name can be at most 80 characters.")
    try:
        with transaction.atomic():
            if ExpenseCategory.objects.filter(name__iexact=name).exists():
                raise ExpenseError("An expense category with that name already exists.")
            return ExpenseCategory.objects.create(name=name)
    except IntegrityError:
        if ExpenseCategory.objects.filter(name__iexact=name).exists():
            raise ExpenseError("An expense category with that name already exists.")
        raise


def rename_category(category_id, name):
    name = str(name or "").strip()
    if not name:
        raise ExpenseError("Category name is required.")
    if len(name) > 80:
        raise ExpenseError("Category name can be at most 80 characters.")
    try:
        with transaction.atomic():
            category = ExpenseCategory.objects.select_for_update().filter(pk=category_id).first()
            if category is None:
                raise ExpenseError("Expense category not found.", status=404)
            if ExpenseCategory.objects.filter(name__iexact=name).exclude(pk=category.pk).exists():
                raise ExpenseError("An expense category with that name already exists.")
            category.name = name
            category.save(update_fields=["name", "updated_at"])
    except IntegrityError:
        if ExpenseCategory.objects.filter(name__iexact=name).exists():
            raise ExpenseError("An expense category with that name already exists.")
        raise
    return category


def set_category_active(category_id, is_active):
    with transaction.atomic():
        category = ExpenseCategory.objects.select_for_update().filter(pk=category_id).first()
        if category is None:
            raise ExpenseError("Expense category not found.", status=404)
        category.is_active = bool(is_active)
        category.save(update_fields=["is_active", "updated_at"])
    return category


def toggle_category_active(category_id):
    with transaction.atomic():
        category = ExpenseCategory.objects.select_for_update().filter(pk=category_id).first()
        if category is None:
            raise ExpenseError("Expense category not found.", status=404)
        category.is_active = not category.is_active
        category.save(update_fields=["is_active", "updated_at"])
    return category


def delete_category(category_id):
    with transaction.atomic():
        category = ExpenseCategory.objects.select_for_update().filter(pk=category_id).first()
        if category is None:
            raise ExpenseError("Expense category not found.", status=404)
        if category.expenses.exists():
            raise ExpenseError("A category with expenses cannot be deleted. Deactivate it instead.")
        category.delete()
