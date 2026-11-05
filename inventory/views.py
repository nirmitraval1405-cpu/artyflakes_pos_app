import csv
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, UpdateView

from core.permissions import AdminRequiredMixin

from .forms import CategoryForm, ProductForm
from .models import Category, Product
from .utils.barcode_generator import (
    create_barcode_image,
    generate_barcode,
    get_barcode_image_name,
    validate_barcode,
)

LIST_URL = reverse_lazy("inventory:product_list")


@login_required
def product_list(request):
    q = request.GET.get("q", "").strip()
    group = request.GET.get("group", "")

    products = Product.objects.select_related("category")
    selected_category = None

    if group.isdigit():
        selected_category = Category.objects.filter(pk=group).first()
        if selected_category:
            products = products.filter(category=selected_category)
    elif group == "none":
        products = products.filter(category__isnull=True)

    if q:
        products = products.filter(
            Q(name__icontains=q) | Q(code__icontains=q) | Q(barcode__icontains=q)
        )

    paginator = Paginator(products.order_by("name"), 50)
    page = paginator.get_page(request.GET.get("page"))

    params = request.GET.copy()
    params.pop("page", None)

    context = {
        "page": page,
        "total": paginator.count,
        "q": q,
        "group": group,
        "selected_category": selected_category,
        "categories": Category.objects.annotate(n=Count("products")),
        "all_count": Product.objects.count(),
        "ungrouped_count": Product.objects.filter(category__isnull=True).count(),
        "querystring": params.urlencode(),
        "can_edit": request.user.is_admin_role,
    }
    return render(request, "inventory/product_list.html", context)


def _safe(value):
    """Stop spreadsheet apps from running a cell that starts with = + - @"""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


@login_required
def product_export(request):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="products.csv"'
    writer = csv.writer(response)
    writer.writerow(["Code", "Name", "Group", "Barcode", "Unit", "Cost", "Sale price", "MRP", "GST %", "HSN", "Low stock level", "Active"])
    for p in Product.objects.select_related("category").order_by("name"):
        writer.writerow([
            _safe(p.code), _safe(p.name), _safe(p.category.name if p.category else ""),
            _safe(p.barcode), p.unit, p.cost_price, p.sale_price, p.mrp, p.gst_rate,
            _safe(p.hsn_code), p.min_stock, "Yes" if p.is_active else "No",
        ])
    return response


# ---------- Create / edit pages (admin only) ----------
class FormPage(AdminRequiredMixin):
    template_name = "inventory/form.html"
    success_url = LIST_URL
    saved_message = "Saved."

    def form_valid(self, form):
        messages.success(self.request, self.saved_message)
        return super().form_valid(form)


class ProductFormPage(FormPage):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["product_form"] = True
        return context

    def form_valid(self, form):
        barcode = form.cleaned_data.get("barcode")
        if not barcode:
            barcode = generate_barcode(sku=form.cleaned_data.get("code"))
        else:
            barcode = validate_barcode(barcode, exclude_pk=form.instance.pk)

        old_image_name = form.instance.barcode_image.name
        image_name = get_barcode_image_name(barcode)
        if not default_storage.exists(image_name):
            image_name = create_barcode_image(barcode)

        form.instance.barcode = barcode
        form.instance.barcode_image.name = image_name
        try:
            with transaction.atomic():
                self.object = form.save()
        except IntegrityError:
            form.add_error("barcode", "This barcode is already assigned to another product.")
            return self.form_invalid(form)

        if old_image_name and old_image_name != image_name:
            default_storage.delete(old_image_name)
        messages.success(self.request, self.saved_message)
        return HttpResponseRedirect(self.get_success_url())


class ProductCreate(ProductFormPage, CreateView):
    model = Product
    form_class = ProductForm
    saved_message = "Product added."
    extra_context = {"title": "New product", "back_url": LIST_URL}


class ProductUpdate(ProductFormPage, UpdateView):
    model = Product
    form_class = ProductForm
    saved_message = "Product updated."
    extra_context = {"title": "Edit product", "back_url": LIST_URL}


@login_required
@require_POST
def generate_product_barcode(request):
    if not request.user.is_admin_role:
        return JsonResponse({"error": "You do not have permission to generate barcodes."}, status=403)

    sku = request.POST.get("code", "").strip()
    try:
        barcode = generate_barcode(sku=sku or None)
    except ValidationError as exc:
        return JsonResponse({"error": exc.messages[0]}, status=400)

    image_name = create_barcode_image(barcode)
    return JsonResponse({
        "barcode": barcode,
        "image_url": default_storage.url(image_name),
    })


class CategoryCreate(FormPage, CreateView):
    model = Category
    form_class = CategoryForm
    saved_message = "Group added."
    extra_context = {"title": "New group", "back_url": LIST_URL}


class CategoryUpdate(FormPage, UpdateView):
    model = Category
    form_class = CategoryForm
    saved_message = "Group updated."
    extra_context = {"title": "Edit group", "back_url": LIST_URL}


# ---------- Delete pages (admin only) ----------
class DeletePage(AdminRequiredMixin):
    template_name = "inventory/confirm_delete.html"
    success_url = LIST_URL

    def form_valid(self, form):
        messages.success(self.request, f'"{self.object}" deleted.')
        return super().form_valid(form)


class ProductDelete(DeletePage, DeleteView):
    model = Product
    extra_context = {"title": "Delete product", "back_url": LIST_URL, "warning": ""}


class CategoryDelete(DeletePage, DeleteView):
    model = Category
    extra_context = {
        "title": "Delete group",
        "back_url": LIST_URL,
        "warning": "Products in this group are kept and move to \"No group\".",
    }