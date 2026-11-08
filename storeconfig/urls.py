from django.urls import path

from . import views

app_name = "storeconfig"

urlpatterns = [
    path("store/", views.StoreSettingsView.as_view(), name="store"),
]