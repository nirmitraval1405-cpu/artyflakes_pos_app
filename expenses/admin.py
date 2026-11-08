from django.contrib import admin

from .models import Expense, ExpenseCategory, ExpenseCounter


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class ExpenseInline(ReadOnlyInline):
    model = Expense
    fields = ("expense_no", "expense_date", "amount", "payment_method", "status", "created_by")
    readonly_fields = fields

    def has_change_permission(self, request, obj=None):
        return False


class ReadOnlyModelAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_staff


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(ReadOnlyModelAdmin):
    list_display = ("name", "is_active", "created_at")
    search_fields = ("name",)
    readonly_fields = ("name", "is_active", "created_at", "updated_at")
    inlines = (ExpenseInline,)


@admin.register(Expense)
class ExpenseAdmin(ReadOnlyModelAdmin):
    list_display = ("expense_no", "expense_date", "category", "amount", "payment_method", "status")
    list_filter = ("status", "payment_method", "category", "expense_date")
    search_fields = ("expense_no", "payee", "reference", "note")
    readonly_fields = (
        "expense_no", "expense_date", "category", "amount", "payment_method", "payee",
        "reference", "note", "status", "voided_at", "voided_by", "void_reason",
        "created_by", "client_token", "created_at", "updated_at",
    )


@admin.register(ExpenseCounter)
class ExpenseCounterAdmin(ReadOnlyModelAdmin):
    list_display = ("year", "last_number")
    readonly_fields = ("year", "last_number")
