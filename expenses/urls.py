from django.urls import path

from . import views

app_name = "expenses"

urlpatterns = [
    path("", views.ExpenseList.as_view(), name="list"),
    path("new/", views.ExpenseCreate.as_view(), name="create"),
    path("export/", views.ExpenseExport.as_view(), name="export"),
    path("categories/", views.ExpenseCategoryList.as_view(), name="categories"),
    path("categories/new/", views.ExpenseCategoryCreate.as_view(), name="category_create"),
    path("categories/<int:pk>/edit/", views.ExpenseCategoryUpdate.as_view(), name="category_update"),
    path("categories/<int:pk>/toggle/", views.ExpenseCategoryToggle.as_view(), name="category_toggle"),
    path("categories/<int:pk>/delete/", views.ExpenseCategoryDelete.as_view(), name="category_delete"),
    path("<int:pk>/", views.ExpenseDetail.as_view(), name="detail"),
    path("<int:pk>/void/", views.ExpenseVoid.as_view(), name="void"),
]
