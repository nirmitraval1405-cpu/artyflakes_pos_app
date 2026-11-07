from django.urls import path

from . import views

app_name = "purchase"

urlpatterns = [
    path("", views.purchase_list, name="list"),
    path("export/", views.purchase_export, name="export"),
    path("new/", views.PurchaseCreate.as_view(), name="create"),
    path("<int:pk>/", views.purchase_detail, name="detail"),
    path("<int:pk>/payment/", views.PurchasePaymentCreate.as_view(), name="payment"),
    path("<int:pk>/cancel/", views.purchase_cancel, name="cancel"),
]
