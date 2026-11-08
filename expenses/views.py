import csv
import uuid
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from core.permissions import AdminRequiredMixin

from .forms import ExpenseCategoryForm, ExpenseForm, ExpenseVoidForm
from .models import Expense, ExpenseCategory
from .services import (
    ExpenseError,
    create_category,
    create_expense,
    delete_category,
    ensure_default_categories,
    rename_category,
    toggle_category_active,
    void_expense,
)


def _safe(value):
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _expense_queryset(request):
    qs = Expense.objects.select_related("category", "created_by", "voided_by")
    query = request.GET.get("q", "").strip()
    if query:
        qs = qs.filter(
            Q(expense_no__icontains=query)
            | Q(payee__icontains=query)
            | Q(note__icontains=query)
            | Q(reference__icontains=query)
        )

    category_id = request.GET.get("category", "").strip()
    if category_id:
        if category_id.isdigit() and ExpenseCategory.objects.filter(pk=category_id).exists():
            qs = qs.filter(category_id=category_id)
        else:
            messages.error(request, "Choose a valid expense category filter.")
            category_id = ""

    method = request.GET.get("method", "").strip()
    if method:
        if method in Expense.PaymentMethod.values:
            qs = qs.filter(payment_method=method)
        else:
            messages.error(request, "Choose a valid payment method filter.")
            method = ""

    status = request.GET.get("status", "").strip()
    if status:
        if status in Expense.Status.values:
            qs = qs.filter(status=status)
        else:
            messages.error(request, "Choose a valid status filter.")
            status = ""

    start_text = request.GET.get("from_date", "").strip()
    end_text = request.GET.get("to_date", "").strip()
    start = end = None
    for raw, label in ((start_text, "Start date"), (end_text, "End date")):
        if not raw:
            continue
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            messages.error(request, f"{label} is not a valid date.")
            continue
        if label == "Start date":
            start = parsed
        else:
            end = parsed
    if start and end and start > end:
        messages.error(request, "Start date must be on or before end date.")
        start = end = None
    if start:
        qs = qs.filter(expense_date__gte=start)
    if end:
        qs = qs.filter(expense_date__lte=end)
    return qs, {
        "q": query,
        "category_id": category_id,
        "method": method,
        "status": status,
        "from_date": start_text,
        "to_date": end_text,
    }


def _active_summary(qs):
    summary = qs.filter(status=Expense.Status.ACTIVE).aggregate(count=Count("id"), total=Sum("amount"))
    summary["total"] = summary["total"] or Decimal("0.00")
    return summary


class ExpenseList(AdminRequiredMixin, View):
    def get(self, request):
        ensure_default_categories()
        qs, filters = _expense_queryset(request)
        summary = _active_summary(qs)
        paginator = Paginator(qs.order_by("-expense_date", "-id"), 25)
        page = paginator.get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        return render(request, "expenses/list.html", {
            **filters,
            "page": page,
            "total": paginator.count,
            "querystring": params.urlencode(),
            "active_count": summary["count"],
            "active_total": summary["total"],
            "categories": ExpenseCategory.objects.order_by("name"),
            "methods": Expense.PaymentMethod.choices,
            "statuses": Expense.Status.choices,
        })


class ExpenseCreate(AdminRequiredMixin, View):
    template_name = "expenses/form.html"

    def get(self, request):
        ensure_default_categories()
        form = ExpenseForm(initial={
            "expense_date": timezone.localdate(),
            "payment_method": Expense.PaymentMethod.CASH,
            "client_token": uuid.uuid4().hex,
        })
        return render(request, self.template_name, {"form": form, "title": "New expense"})

    def post(self, request):
        ensure_default_categories()
        form = ExpenseForm(request.POST)
        if form.is_valid():
            try:
                expense, duplicate = create_expense(request.user, form.cleaned_data)
            except ExpenseError as exc:
                messages.error(request, str(exc))
            else:
                if duplicate:
                    messages.info(request, f"Expense {expense.expense_no} was already saved.")
                else:
                    messages.success(request, f"Expense {expense.expense_no} saved.")
                return redirect("expenses:detail", pk=expense.pk)
        return render(request, self.template_name, {"form": form, "title": "New expense"})


class ExpenseDetail(AdminRequiredMixin, View):
    def get(self, request, pk):
        expense = get_object_or_404(
            Expense.objects.select_related("category", "created_by", "voided_by"), pk=pk
        )
        return render(request, "expenses/detail.html", {
            "expense": expense,
            "void_form": ExpenseVoidForm(),
        })


class ExpenseVoid(AdminRequiredMixin, View):
    def post(self, request, pk):
        expense = get_object_or_404(Expense, pk=pk)
        form = ExpenseVoidForm(request.POST)
        if not form.is_valid():
            for error in form.errors.get("reason", []):
                messages.error(request, error)
            return redirect("expenses:detail", pk=expense.pk)
        try:
            void_expense(request.user, expense.pk, form.cleaned_data["reason"])
        except ExpenseError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Expense {expense.expense_no} was voided.")
        return redirect("expenses:detail", pk=expense.pk)


class ExpenseExport(AdminRequiredMixin, View):
    def get(self, request):
        qs, _ = _expense_queryset(request)
        summary = _active_summary(qs)
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="expenses.csv"'
        writer = csv.writer(response)
        writer.writerow([
            "Expense no", "Date", "Category", "Amount", "Payment method",
            "Payee", "Reference", "Note", "Status", "Created by",
        ])
        for expense in qs.order_by("-expense_date", "-id"):
            writer.writerow([
                _safe(expense.expense_no),
                expense.expense_date.isoformat(),
                _safe(expense.category.name),
                expense.amount,
                _safe(expense.get_payment_method_display()),
                _safe(expense.payee),
                _safe(expense.reference),
                _safe(expense.note),
                _safe(expense.get_status_display()),
                _safe(expense.created_by.get_full_name() or expense.created_by.username),
            ])
        writer.writerow([])
        writer.writerow(["Active expenses", summary["count"], "Active expense total", summary["total"]])
        return response


class ExpenseCategoryList(AdminRequiredMixin, View):
    def get(self, request):
        ensure_default_categories()
        categories = ExpenseCategory.objects.annotate(expense_count=Count("expenses")).order_by("name")
        return render(request, "expenses/categories.html", {"categories": categories})


class ExpenseCategoryCreate(AdminRequiredMixin, View):
    def get(self, request):
        return render(request, "expenses/category_form.html", {
            "form": ExpenseCategoryForm(),
            "title": "New expense category",
            "back_url": reverse("expenses:categories"),
        })

    def post(self, request):
        form = ExpenseCategoryForm(request.POST)
        if form.is_valid():
            try:
                category = create_category(form.cleaned_data["name"])
            except ExpenseError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Category {category.name} created.")
                return redirect("expenses:categories")
        return render(request, "expenses/category_form.html", {
            "form": form,
            "title": "New expense category",
            "back_url": reverse("expenses:categories"),
        })


class ExpenseCategoryUpdate(AdminRequiredMixin, View):
    def get(self, request, pk):
        category = get_object_or_404(ExpenseCategory, pk=pk)
        form = ExpenseCategoryForm(initial={"name": category.name})
        return render(request, "expenses/category_form.html", {
            "form": form,
            "title": "Rename expense category",
            "back_url": reverse("expenses:categories"),
        })

    def post(self, request, pk):
        category = get_object_or_404(ExpenseCategory, pk=pk)
        form = ExpenseCategoryForm(request.POST)
        if form.is_valid():
            try:
                category = rename_category(category.pk, form.cleaned_data["name"])
            except ExpenseError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Category renamed to {category.name}.")
                return redirect("expenses:categories")
        return render(request, "expenses/category_form.html", {
            "form": form,
            "title": "Rename expense category",
            "back_url": reverse("expenses:categories"),
        })


class ExpenseCategoryToggle(AdminRequiredMixin, View):
    def post(self, request, pk):
        get_object_or_404(ExpenseCategory, pk=pk)
        try:
            category = toggle_category_active(pk)
        except ExpenseError as exc:
            messages.error(request, str(exc))
        else:
            state = "activated" if category.is_active else "deactivated"
            messages.success(request, f"Category {category.name} {state}.")
        return redirect("expenses:categories")


class ExpenseCategoryDelete(AdminRequiredMixin, View):
    def get(self, request, pk):
        category = get_object_or_404(ExpenseCategory, pk=pk)
        return render(request, "expenses/category_delete.html", {"category": category})

    def post(self, request, pk):
        category = get_object_or_404(ExpenseCategory, pk=pk)
        try:
            delete_category(category.pk)
        except ExpenseError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Category {category.name} deleted.")
        return redirect("expenses:categories")
