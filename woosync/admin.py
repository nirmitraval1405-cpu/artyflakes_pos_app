from django.contrib import admin

from .models import SyncRun, SyncState, WebOrder, WebOrderItem, WooLink


class ReadOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SyncState)
class SyncStateAdmin(ReadOnlyAdmin):
    list_display = (
        "is_enabled", "running_since", "last_run_at", "last_ok_at", "orders_since",
        "orders_cursor", "last_ok", "last_message",
    )


@admin.register(SyncRun)
class SyncRunAdmin(ReadOnlyAdmin):
    list_display = (
        "started_at", "trigger", "ok", "orders_seen", "orders_new",
        "orders_failed", "stock_pushed",
    )
    list_filter = ("ok", "trigger")
    search_fields = ("summary",)


class WebOrderItemInline(admin.TabularInline):
    model = WebOrderItem
    extra = 0
    can_delete = False
    readonly_fields = tuple(field.name for field in WebOrderItem._meta.fields)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(WebOrder)
class WebOrderAdmin(ReadOnlyAdmin):
    list_display = ("number", "woo_id", "status", "woo_created", "customer_name", "handled", "total")
    list_filter = ("status", "handled", "is_paid")
    search_fields = ("number", "woo_id", "customer_name", "phone", "email")
    inlines = (WebOrderItemInline,)


@admin.register(WebOrderItem)
class WebOrderItemAdmin(ReadOnlyAdmin):
    list_display = ("name", "order", "sku", "quantity", "applied_qty", "product")
    search_fields = ("name", "sku", "order__number")


@admin.register(WooLink)
class WooLinkAdmin(ReadOnlyAdmin):
    list_display = ("product", "woo_product_id", "woo_type", "sell_online", "last_pushed_qty", "last_pushed_at")
    list_filter = ("sell_online", "woo_type")
    search_fields = ("product__name", "product__code", "woo_product_id__exact")
