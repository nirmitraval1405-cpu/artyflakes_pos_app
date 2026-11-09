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
    path("categories/", views.CategoryList.as_view(), name="category_list"),
    path("categories/new/", views.CategoryCreate.as_view(), name="category_create"),
    path("categories/<int:pk>/edit/", views.CategoryUpdate.as_view(), name="category_update"),
    path("categories/<int:pk>/toggle/", views.CategoryToggle.as_view(), name="category_toggle"),
    path("categories/<int:pk>/delete/", views.CategoryDelete.as_view(), name="category_delete"),
    path("brands/", views.BrandList.as_view(), name="brand_list"),
    path("brands/new/", views.BrandCreate.as_view(), name="brand_create"),
    path("brands/<int:pk>/edit/", views.BrandUpdate.as_view(), name="brand_update"),
    path("brands/<int:pk>/toggle/", views.BrandToggle.as_view(), name="brand_toggle"),
    path("brands/<int:pk>/delete/", views.BrandDelete.as_view(), name="brand_delete"),
    path("units/", views.UnitList.as_view(), name="unit_list"),
    path("units/new/", views.UnitCreate.as_view(), name="unit_create"),
    path("units/<int:pk>/edit/", views.UnitUpdate.as_view(), name="unit_update"),
    path("units/<int:pk>/toggle/", views.UnitToggle.as_view(), name="unit_toggle"),
    path("units/<int:pk>/delete/", views.UnitDelete.as_view(), name="unit_delete"),
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