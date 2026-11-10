from django.contrib import admin

from .models import PrintSettings, StoreSettings


@admin.register(StoreSettings)
class StoreSettingsAdmin(admin.ModelAdmin):
    """Read-only here. The Store Settings page is the only editor."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PrintSettings)
class PrintSettingsAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False