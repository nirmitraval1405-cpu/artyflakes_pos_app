import csv

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, TemplateView, UpdateView

from core.permissions import AdminRequiredMixin

from .forms import ContactForm
from .models import Contact

LIST_URL = reverse_lazy("people:contact_list")

SEARCH_FIELDS = {
    "name": "Name",
    "code": "Code",
    "phone": "Phone number",
    "gstin": "Tax number",
    "email": "Email",
    "address": "Address",
}


def _safe(value):
    """Stop spreadsheet apps from running a cell that starts with = + - @"""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


class ContactList(AdminRequiredMixin, TemplateView):
    template_name = "people/list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        Contact.walk_in()  # make sure the built-in row exists

        q = self.request.GET.get("q", "").strip()
        field = self.request.GET.get("field", "name")
        if field not in SEARCH_FIELDS:
            field = "name"
        kind = self.request.GET.get("type", "")

        qs = Contact.objects.all()
        if kind == "customer":
            qs = qs.filter(is_customer=True)
        elif kind == "supplier":
            qs = qs.filter(is_supplier=True)
        if q:
            qs = qs.filter(**{f"{field}__icontains": q})

        paginator = Paginator(qs.order_by("-is_system", "name"), 50)
        page = paginator.get_page(self.request.GET.get("page"))

        rows = []
        for c in page:
            rows.append({
                "pk": c.pk,
                "cells": [
                    c.code or "", c.name, c.gstin or "(none)" if not c.is_system else "(none)",
                    " ".join(c.address.split())[:70] or "(none)",
                    c.country, c.phone or "(none)", c.email or "(none)", c.type_label,
                ],
                "inactive": not c.is_active,
                "edit": "" if c.is_system else reverse("people:contact_update", args=[c.pk]),
                "delete": "" if c.is_system else reverse("people:contact_delete", args=[c.pk]),
            })

        params = self.request.GET.copy()
        params.pop("page", None)

        ctx.update(
            rows=rows, page=page, total=paginator.count, q=q, field=field,
            search_fields=SEARCH_FIELDS, kind=kind, querystring=params.urlencode(),
        )
        return ctx


class ContactExport(AdminRequiredMixin, View):
    def get(self, request):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="customers-suppliers.csv"'
        writer = csv.writer(response)
        writer.writerow(["Code", "Name", "Type", "Tax number", "Address", "City", "Country", "Phone", "Email", "Notes", "Active"])
        for c in Contact.objects.filter(is_system=False):
            writer.writerow([
                _safe(c.code), _safe(c.name), c.type_label, _safe(c.gstin), _safe(c.address),
                _safe(c.city), _safe(c.country), _safe(c.phone), _safe(c.email), _safe(c.notes),
                "Yes" if c.is_active else "No",
            ])
        return response


class ContactCreate(AdminRequiredMixin, CreateView):
    model = Contact
    form_class = ContactForm
    template_name = "inventory/form.html"      # shared generic form page
    success_url = LIST_URL
    extra_context = {"title": "Add customer / supplier", "back_url": LIST_URL}

    def form_valid(self, form):
        messages.success(self.request, "Saved.")
        return super().form_valid(form)


class ContactUpdate(AdminRequiredMixin, UpdateView):
    form_class = ContactForm
    template_name = "inventory/form.html"
    success_url = LIST_URL
    extra_context = {"title": "Edit customer / supplier", "back_url": LIST_URL}

    def get_queryset(self):
        return Contact.objects.filter(is_system=False)   # Walk-in customer cannot be edited

    def form_valid(self, form):
        messages.success(self.request, "Updated.")
        return super().form_valid(form)


class ContactDelete(AdminRequiredMixin, DeleteView):
    template_name = "inventory/confirm_delete.html"
    success_url = LIST_URL
    extra_context = {"title": "Delete customer / supplier", "back_url": LIST_URL, "warning": ""}

    def get_queryset(self):
        return Contact.objects.filter(is_system=False)   # Walk-in customer cannot be deleted

    def form_valid(self, form):
        messages.success(self.request, f'"{self.object}" deleted.')
        return super().form_valid(form)