from django.urls import path

from . import views

app_name = "pos"

urlpatterns = [
    path("", views.screen, name="screen"),
    path("api/products/", views.product_search, name="product_search"),
    path("api/customers/", views.customer_search, name="customer_search"),
    path("api/checkout/", views.checkout, name="checkout"),
]