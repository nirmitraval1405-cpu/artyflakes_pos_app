from django.urls import path

from . import views

app_name = "woosync"

urlpatterns = [
    path("", views.home, name="home"),
    path("webhook/products/", views.product_webhook, name="product_webhook"),
    path("test/", views.test_connection, name="test"),
    path("toggle/", views.toggle, name="toggle"),
    path("settings/", views.save_settings, name="settings"),
    path("match/", views.match_view, name="match"),
    path("sync/", views.sync_now, name="sync"),
    path("products/", views.products, name="products"),
    path("products/conflicts/<int:pk>/resolve/", views.resolve_product_conflict, name="resolve_conflict"),
    path("preview/", views.preview, name="preview"),
    path("log/", views.log_page, name="log"),
    path("orders/", views.orders, name="orders"),
    path("orders/<int:pk>/", views.order_detail, name="order_detail"),
    path("orders/<int:pk>/handled/", views.order_handled, name="order_handled"),
    path("orders/<int:pk>/recheck/", views.order_recheck, name="order_recheck"),
]