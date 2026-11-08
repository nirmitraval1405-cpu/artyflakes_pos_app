import csv
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib import admin
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User

from .admin import ExpenseAdmin, ExpenseCategoryAdmin, ExpenseCounterAdmin
from .models import Expense, ExpenseCategory, ExpenseCounter
from .services import (
    ExpenseError,
    create_category,
    create_expense,
    delete_category,
    ensure_default_categories,
    rename_category,
    set_category_active,
    void_expense,
)


class ExpenseServiceTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("expense_admin", "password", role="admin")
        self.category = ExpenseCategory.objects.create(name="Office")
        self.data = {
            "expense_date": timezone.localdate(),
            "category": self.category.pk,
            "amount": "125.50",
            "payment_method": Expense.PaymentMethod.CASH,
            "payee": "Stationery shop",
            "reference": "",
            "note": "",
            "client_token": "",
        }

    def create(self, **overrides):
        return create_expense(self.admin, {**self.data, **overrides})

    def test_expense_numbers_increment_within_the_year(self):
        first, _ = self.create(client_token="number-1")
        second, _ = self.create(client_token="number-2")
        year = timezone.localdate().year
        self.assertEqual(first.expense_no, f"AF-E-{year}-0001")
        self.assertEqual(second.expense_no, f"AF-E-{year}-0002")

    def test_invalid_amounts_are_rejected(self):
        for value in ("0", "-1", "1.001", "1.230", "100000001", "abc", "NaN", "Infinity"):
            with self.subTest(value=value), self.assertRaises(ExpenseError):
                self.create(amount=value)
        self.assertEqual(Expense.objects.count(), 0)

    def test_future_date_is_rejected(self):
        future = timezone.localdate() + timedelta(days=1)
        with self.assertRaises(ExpenseError):
            self.create(expense_date=future)

    def test_unknown_payment_method_is_rejected(self):
        with self.assertRaises(ExpenseError):
            self.create(payment_method="cheque")

    def test_duplicate_client_token_returns_the_existing_expense(self):
        first, duplicate = self.create(client_token="same-expense-token")
        second, duplicate_again = self.create(
            client_token="same-expense-token", amount="999.00", payee="Ignored duplicate"
        )
        self.assertFalse(duplicate)
        self.assertTrue(duplicate_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Expense.objects.count(), 1)

    def test_integrity_error_race_returns_the_winning_expense(self):
        winner, _ = self.create(client_token="raced-expense-token")
        original_filter = Expense.objects.filter
        query_count = 0

        def filter_with_race(*args, **kwargs):
            nonlocal query_count
            if kwargs.get("client_token") == "raced-expense-token":
                query_count += 1
                if query_count == 1:
                    return Expense.objects.none()
            return original_filter(*args, **kwargs)

        with patch.object(Expense.objects, "filter", side_effect=filter_with_race):
            with patch.object(Expense.objects, "create", side_effect=IntegrityError("duplicate token")):
                returned, duplicate = self.create(client_token="raced-expense-token")
        self.assertTrue(duplicate)
        self.assertEqual(returned.pk, winner.pk)
        self.assertEqual(Expense.objects.count(), 1)

    def test_void_requires_a_reason_and_can_only_happen_once(self):
        expense, _ = self.create()
        with self.assertRaises(ExpenseError):
            void_expense(self.admin, expense.pk, "")
        voided = void_expense(self.admin, expense.pk, "Entered against wrong receipt")
        self.assertEqual(voided.status, Expense.Status.VOIDED)
        self.assertEqual(voided.voided_by, self.admin)
        with self.assertRaises(ExpenseError):
            void_expense(self.admin, expense.pk, "Try again")

    def test_category_with_expenses_cannot_be_deleted(self):
        self.create()
        with self.assertRaises(ExpenseError):
            delete_category(self.category.pk)
        self.assertTrue(ExpenseCategory.objects.filter(pk=self.category.pk).exists())

    def test_category_without_expenses_can_be_deleted(self):
        category = create_category("Temporary")
        delete_category(category.pk)
        self.assertFalse(ExpenseCategory.objects.filter(pk=category.pk).exists())

    def test_category_can_be_renamed_and_activated_or_deactivated(self):
        category = rename_category(self.category.pk, "Office supplies")
        self.assertEqual(category.name, "Office supplies")
        category = set_category_active(category.pk, False)
        self.assertFalse(category.is_active)
        category = set_category_active(category.pk, True)
        self.assertTrue(category.is_active)

    def test_default_categories_are_created_lazily(self):
        ensure_default_categories()
        self.assertTrue(ExpenseCategory.objects.filter(name="Rent", is_active=True).exists())
        self.assertTrue(ExpenseCategory.objects.filter(name="Miscellaneous", is_active=True).exists())

    def test_inactive_category_cannot_be_used_for_new_expense(self):
        set_category_active(self.category.pk, False)
        with self.assertRaises(ExpenseError):
            self.create()


class ExpenseViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("expense_view_admin", "password", role="admin")
        self.cashier = User.objects.create_user("expense_cashier", "password", role="cashier")
        self.category = ExpenseCategory.objects.create(name="Travel")
        self.client.force_login(self.admin)

    def create_expense(self, amount="25.00", token=None, **extra):
        return create_expense(self.admin, {
            "expense_date": timezone.localdate(),
            "category": self.category.pk,
            "amount": amount,
            "payment_method": Expense.PaymentMethod.CASH,
            "payee": extra.pop("payee", "Taxi"),
            "reference": "",
            "note": extra.pop("note", ""),
            "client_token": token,
            **extra,
        })[0]

    def test_form_has_token_and_saving_redirects_to_detail(self):
        response = self.client.get(reverse("expenses:create"))
        self.assertContains(response, 'name="client_token"')
        token = response.context["form"]["client_token"].value()
        response = self.client.post(reverse("expenses:create"), {
            "expense_date": timezone.localdate().isoformat(),
            "category": self.category.pk,
            "amount": "25.00",
            "payment_method": "cash",
            "payee": "Taxi",
            "client_token": token,
        })
        expense = Expense.objects.get()
        self.assertRedirects(response, reverse("expenses:detail", args=[expense.pk]))

    def test_list_total_excludes_voided_expenses(self):
        self.create_expense(amount="25.00", token="list-active")
        voided = self.create_expense(amount="70.00", token="list-void")
        void_expense(self.admin, voided.pk, "Incorrect")
        response = self.client.get(reverse("expenses:list"))
        self.assertEqual(response.context["total"], 2)
        self.assertEqual(response.context["active_count"], 1)
        self.assertEqual(response.context["active_total"], Decimal("25.00"))

    def test_inactive_category_is_not_in_new_form_but_old_expense_displays(self):
        expense = self.create_expense(token="inactive-old")
        set_category_active(self.category.pk, False)
        form_response = self.client.get(reverse("expenses:create"))
        self.assertNotIn(self.category, form_response.context["form"].fields["category"].queryset)
        detail_response = self.client.get(reverse("expenses:detail", args=[expense.pk]))
        self.assertContains(detail_response, "Travel")

    def test_export_neutralises_formula_payee_and_summary_excludes_voids(self):
        self.create_expense(amount="25.00", token="csv-active", payee="=SUM(1,1)")
        voided = self.create_expense(amount="70.00", token="csv-void", payee="Voided")
        void_expense(self.admin, voided.pk, "Wrong")
        response = self.client.get(reverse("expenses:export"))
        rows = list(csv.reader(StringIO(response.content.decode("utf-8"))))
        self.assertIn("'=SUM(1,1)", [cell for row in rows for cell in row])
        self.assertEqual(rows[-1], ["Active expenses", "1", "Active expense total", "25.00"])

    def test_pager_querystring_keeps_filters(self):
        for index in range(26):
            self.create_expense(token=f"pager-{index}", payee="Match")
        response = self.client.get(reverse("expenses:list"), {"q": "Match", "method": "cash"})
        self.assertEqual(response.context["page"].paginator.num_pages, 2)
        self.assertIn("q=Match", response.context["querystring"])
        self.assertIn("method=cash", response.context["querystring"])
        self.assertNotIn("page=", response.context["querystring"])

    def test_cashier_cannot_open_expense_pages_or_export(self):
        expense = self.create_expense(token="cashier-permission")
        urls = [
            reverse("expenses:list"),
            reverse("expenses:create"),
            reverse("expenses:detail", args=[expense.pk]),
            reverse("expenses:export"),
            reverse("expenses:categories"),
            reverse("expenses:category_create"),
            reverse("expenses:category_update", args=[self.category.pk]),
            reverse("expenses:category_delete", args=[self.category.pk]),
        ]
        self.client.force_login(self.cashier)
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("expenses:void", args=[expense.pk]), {"reason": "Not allowed"}).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse("expenses:category_toggle", args=[self.category.pk])).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse("expenses:category_delete", args=[self.category.pk])).status_code,
            403,
        )

    def test_expense_admin_models_are_read_only(self):
        expense_admin = admin.site._registry[Expense]
        category_admin = admin.site._registry[ExpenseCategory]
        counter_admin = admin.site._registry[ExpenseCounter]
        self.assertIsInstance(expense_admin, ExpenseAdmin)
        self.assertIsInstance(category_admin, ExpenseCategoryAdmin)
        self.assertFalse(expense_admin.has_add_permission(None))
        self.assertFalse(expense_admin.has_change_permission(None))
        self.assertFalse(expense_admin.has_delete_permission(None))
        self.assertFalse(category_admin.has_add_permission(None))
        self.assertFalse(category_admin.has_change_permission(None))
        self.assertFalse(category_admin.has_delete_permission(None))
        self.assertIsInstance(counter_admin, ExpenseCounterAdmin)
        self.assertFalse(counter_admin.has_add_permission(None))
        self.assertFalse(counter_admin.has_change_permission(None))
        self.assertFalse(counter_admin.has_delete_permission(None))
