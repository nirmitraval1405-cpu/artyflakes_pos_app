from django.urls import path
from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("sales-statistics/", views.sales_statistics, name="sales_statistics"),
]