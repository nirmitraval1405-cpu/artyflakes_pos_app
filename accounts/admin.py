from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("POS", {"fields": ("role", "phone")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("POS", {"fields": ("role", "phone")}),)
    list_display = ("username", "first_name", "role", "is_active")