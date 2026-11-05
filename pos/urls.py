from django.urls import path
from . import views

app_name = "pos"

urlpatterns = [
    path("", views.screen, name="screen"),
    path("api/products/search/", views.product_search, name="product_search"),
    path("api/pos/checkout/", views.checkout, name="checkout"),
    path("api/pos/hold/", views.hold_sale, name="hold_sale"),
    path("api/pos/held/", views.held_sales, name="held_sales"),
    path("api/pos/held/<int:held_id>/restore/", views.restore_held_sale, name="restore_held_sale"),
]