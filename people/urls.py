from django.urls import path

from . import views

app_name = "people"

urlpatterns = [
    path("contacts/", views.ContactList.as_view(), name="contact_list"),
    path("contacts/export/", views.ContactExport.as_view(), name="contact_export"),
    path("contacts/new/", views.ContactCreate.as_view(), name="contact_create"),
    path("contacts/<int:pk>/edit/", views.ContactUpdate.as_view(), name="contact_update"),
    path("contacts/<int:pk>/delete/", views.ContactDelete.as_view(), name="contact_delete"),
]