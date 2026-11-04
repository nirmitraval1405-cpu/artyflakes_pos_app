from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.ReportIndex.as_view(), name="index"),
    path("view/<slug:slug>/", views.ReportView.as_view(), name="view"),
]