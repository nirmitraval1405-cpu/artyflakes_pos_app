from django.contrib import admin
from .models import Contact


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "phone", "is_customer", "is_supplier", "is_active")
    search_fields = ("name", "phone", "code")
    list_filter = ("is_customer", "is_supplier", "is_active")