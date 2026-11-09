from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.ReportIndex.as_view(), name="index"),
    path("view/<slug:slug>/", views.ReportView.as_view(), name="view"),
    path("gstr1/", views.Gstr1Page.as_view(), name="gstr1"),
    path("gstr1/download/", views.Gstr1Download.as_view(), name="gstr1_download"),
]