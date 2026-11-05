from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("products/", views.product_list, name="product_list"),
    path("products/export/", views.product_export, name="product_export"),
    path("products/barcode/generate/", views.generate_product_barcode, name="barcode_generate"),
    path("products/new/", views.ProductCreate.as_view(), name="product_create"),
    path("products/<int:pk>/edit/", views.ProductUpdate.as_view(), name="product_update"),
    path("products/<int:pk>/delete/", views.ProductDelete.as_view(), name="product_delete"),
    path("groups/new/", views.CategoryCreate.as_view(), name="group_create"),
    path("groups/<int:pk>/edit/", views.CategoryUpdate.as_view(), name="group_update"),
    path("groups/<int:pk>/delete/", views.CategoryDelete.as_view(), name="group_delete"),
]