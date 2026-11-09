from django.urls import path

from . import views

app_name = "promotions"

urlpatterns = [
    path("", views.PromotionList.as_view(), name="list"),
    path("new/", views.PromotionEdit.as_view(), name="create"),
    path("<int:pk>/edit/", views.PromotionEdit.as_view(), name="edit"),
    path("<int:pk>/delete/", views.PromotionDelete.as_view(), name="delete"),
    path("<int:pk>/toggle/", views.promotion_toggle, name="toggle"),
]