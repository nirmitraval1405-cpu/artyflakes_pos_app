from django.urls import path

from . import views

app_name = "pos"

urlpatterns = [
    path("", views.screen, name="screen"),
    path("api/products/", views.product_search, name="product_search"),
    path("api/customers/", views.customer_search, name="customer_search"),
    path("api/checkout/", views.checkout, name="checkout"),
    path("api/sales/<int:pk>/note/", views.sale_note, name="sale_note"),
    path("receipt/<int:pk>/", views.receipt, name="receipt"),
    path("invoice/<int:pk>/", views.invoice, name="invoice"),

    path("sales/", views.sale_list, name="sale_list"),
    path("sales/<int:pk>/", views.sale_detail, name="sale_detail"),
    path("sales/<int:pk>/void/", views.sale_void, name="sale_void"),
    path("sales/<int:pk>/return/", views.sale_return, name="sale_return"),
    path("returns/", views.return_list, name="return_list"),
    path("api/held/", views.held_list, name="held_list"),
    path("api/held/save/", views.held_save, name="held_save"),
    path("api/held/<int:pk>/resume/", views.held_resume, name="held_resume"),
    path("api/held/<int:pk>/delete/", views.held_delete, name="held_delete"),
    path("api/drawer/", views.drawer_open, name="drawer_open"),
    path("api/find-sale/", views.find_sale, name="find_sale"),
    path("api/lock/", views.lock, name="lock"),
    path("api/unlock/", views.unlock, name="unlock"),
]