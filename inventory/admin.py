from django.contrib import admin

from .models import Category, Product, StockMovement

admin.site.register(Category)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category", "sale_price", "track_stock", "is_active")
    search_fields = ("name", "code", "barcode")


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    """Read-only: the stock ledger must never be edited or deleted."""
    list_display = ("created_at", "product", "movement_type", "quantity", "note", "created_by")
    list_filter = ("movement_type",)
    search_fields = ("product__name", "note")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False