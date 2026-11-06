from django.contrib import admin

from .models import Payment, Sale, SaleItem, SaleReturn, SaleReturnItem


class _ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class SaleItemInline(_ReadOnlyInline):
    model = SaleItem
    fields = readonly_fields = ("product_name", "quantity", "unit_price", "discount", "tax_amount", "line_total")


class PaymentInline(_ReadOnlyInline):
    model = Payment
    fields = readonly_fields = ("method", "amount", "reference")


class ReturnItemInline(_ReadOnlyInline):
    model = SaleReturnItem
    fields = readonly_fields = ("product_name", "quantity", "refund_amount", "tax_amount")


class _ReadOnlyAdmin(admin.ModelAdmin):
    """Sales and returns are never edited or deleted. Mistakes are fixed with a void or a return."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Sale)
class SaleAdmin(_ReadOnlyAdmin):
    list_display = ("invoice_no", "created_at", "customer", "total", "status")
    list_filter = ("status",)
    search_fields = ("invoice_no", "customer__name")
    inlines = [SaleItemInline, PaymentInline]


@admin.register(SaleReturn)
class SaleReturnAdmin(_ReadOnlyAdmin):
    list_display = ("return_no", "created_at", "sale", "refund_method", "refund_total")
    search_fields = ("return_no", "sale__invoice_no")
    inlines = [ReturnItemInline]