from django.contrib import admin

from .models import Payment, Sale, SaleItem


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    can_delete = False
    fields = readonly_fields = ("product_name", "quantity", "unit_price", "discount", "tax_amount", "line_total")

    def has_add_permission(self, request, obj=None):
        return False


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    can_delete = False
    fields = readonly_fields = ("method", "amount", "reference")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    """Read-only: sales are never edited or deleted. Mistakes are fixed with a void or return."""
    list_display = ("invoice_no", "created_at", "customer", "total", "status")
    list_filter = ("status",)
    search_fields = ("invoice_no", "customer__name")
    inlines = [SaleItemInline, PaymentInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False