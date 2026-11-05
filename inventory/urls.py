from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("products/", views.product_list, name="product_list"),
    path("products/export/", views.product_export, name="product_export"),
    path("products/new/", views.ProductCreate.as_view(), name="product_create"),
    path("products/<int:pk>/edit/", views.ProductUpdate.as_view(), name="product_update"),
    path("products/<int:pk>/delete/", views.ProductDelete.as_view(), name="product_delete"),
    path("groups/new/", views.CategoryCreate.as_view(), name="group_create"),
    path("groups/<int:pk>/edit/", views.CategoryUpdate.as_view(), name="group_update"),
    path("groups/<int:pk>/delete/", views.CategoryDelete.as_view(), name="group_delete"),

    path("stock/", views.stock_list, name="stock_list"),
    path("stock/export/", views.stock_export, name="stock_export"),
    path("stock/add/", views.StockEntry.as_view(kind="add"), name="stock_add"),
    path("stock/count/", views.StockEntry.as_view(kind="count"), name="stock_count"),
    path("stock/damage/", views.StockEntry.as_view(kind="damage"), name="stock_damage"),
    path("stock/movements/", views.movement_list, name="movement_list"),
]