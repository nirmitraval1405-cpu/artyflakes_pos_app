from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.ReportIndex.as_view(), name="index"),
    path(
        "sales-by-customer/",
        views.ReportView.as_view(),
        {"slug": "customers-sales-by-customer"},
        name="sales_by_customer",
    ),
    path(
        "invoice-list/",
        views.ReportView.as_view(),
        {"slug": "customers-invoice-list"},
        name="invoice_list",
    ),
    path(
        "item-list/",
        views.ReportView.as_view(),
        {"slug": "customers-item-list"},
        name="item_list",
    ),
    path("view/<slug:slug>/", views.ReportView.as_view(), name="view"),
    path("gstr1/", views.Gstr1Page.as_view(), name="gstr1"),
    path("gstr1/download/", views.Gstr1Download.as_view(), name="gstr1_download"),
    path("gstr3b/", views.Gstr3bPage.as_view(), name="gstr3b"),
    path("gstr3b/download/", views.Gstr3bDownload.as_view(), name="gstr3b_download"),
]