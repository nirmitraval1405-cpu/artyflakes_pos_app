from django.contrib import admin

from .models import Purchase, PurchaseCounter, PurchaseItem, PurchasePayment


class _ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class PurchaseItemInline(_ReadOnlyInline):
    model = PurchaseItem
    fields = readonly_fields = ("product_name", "quantity", "cost_price", "discount", "gst_rate", "total_amount")


class PurchasePaymentInline(_ReadOnlyInline):
    model = PurchasePayment
    fields = readonly_fields = ("method", "amount", "payment_date", "reference")


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    """Read-only: purchases are never edited or deleted. Mistakes are fixed by cancelling in the app."""
    list_display = ("purchase_no", "purchase_date", "supplier", "grand_total", "status")
    list_filter = ("status",)
    search_fields = ("purchase_no", "supplier__name", "supplier_invoice_no")
    inlines = [PurchaseItemInline, PurchasePaymentInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(PurchaseCounter)