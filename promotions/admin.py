from django.contrib import admin

from .models import Promotion, PromotionItem


class ItemInline(admin.TabularInline):
    model = PromotionItem
    extra = 0
    can_delete = False
    fields = readonly_fields = ("product", "kind", "value", "min_qty")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    """Read-only here. The Promotions page checks every value, so it is the only editor."""
    list_display = ("name", "is_active", "starts_at", "ends_at")
    inlines = [ItemInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False