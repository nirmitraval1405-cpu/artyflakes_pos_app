import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, ProtectedError, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, FormView, UpdateView

from core.permissions import AdminRequiredMixin

from .forms import AddStockForm, CategoryForm, CountForm, DamageForm, ProductForm
from .models import Category, Product, StockMovement
from .stock import current_stock, low_stock, qty_fmt, stock_status, with_stock
from .utils.barcode_generator import create_barcode_image, generate_barcode

LIST_URL = reverse_lazy("inventory:product_list")
STOCK_URL = reverse_lazy("inventory:stock_list")


def _safe(value):
    """Stop spreadsheet apps from running a cell that starts with = + - @"""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


# =====================  PRODUCTS  =====================
@login_required
def product_list(request):
    q = request.GET.get("q", "").strip()
    group = request.GET.get("group", "")

    products = with_stock(Product.objects.select_related("category"))
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


@login_required
def product_export(request):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="products.csv"'
    writer = csv.writer(response)
    writer.writerow(["Code", "Name", "Group", "Barcode", "Unit", "Cost", "Sale price", "MRP", "GST %", "HSN", "Stock", "Low stock level", "Active"])
    for p in with_stock(Product.objects.select_related("category")).order_by("name"):
        writer.writerow([
            _safe(p.code), _safe(p.name), _safe(p.category.name if p.category else ""),
            _safe(p.barcode), p.unit, p.cost_price, p.sale_price, p.mrp, p.gst_rate,
            _safe(p.hsn_code), qty_fmt(p.stock) if p.track_stock else "", p.min_stock,
            "Yes" if p.is_active else "No",
        ])
    return response


@login_required
@require_POST
def generate_product_barcode(request):
    if not request.user.is_admin_role:
        return JsonResponse({"error": "You do not have permission to generate barcodes."}, status=403)

    try:
        barcode = generate_barcode(sku=request.POST.get("code", "").strip() or None)
    except ValidationError as exc:
        return JsonResponse({"error": exc.messages[0]}, status=400)

    image_name = create_barcode_image(barcode)
    return JsonResponse({
        "barcode": barcode,
        "image_url": default_storage.url(image_name),
    })


# ---------- Create / edit pages (admin only) ----------
class FormPage(AdminRequiredMixin):
    template_name = "inventory/form.html"
    success_url = LIST_URL
    saved_message = "Saved."

    def form_valid(self, form):
        messages.success(self.request, self.saved_message)
        return super().form_valid(form)


class ProductCreate(FormPage, CreateView):
    model = Product
    form_class = ProductForm
    saved_message = "Product added."
    extra_context = {"title": "New product", "back_url": LIST_URL, "product_form": True}

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)
            opening_stock = form.cleaned_data["opening_stock"]
            if form.cleaned_data["track_stock"] and opening_stock > 0:
                StockMovement.objects.create(
                    product=self.object,
                    quantity=opening_stock,
                    movement_type=StockMovement.Type.OPENING,
                    note="Opening stock",
                    created_by=self.request.user,
                )
        return response


class ProductUpdate(FormPage, UpdateView):
    model = Product
    form_class = ProductForm
    saved_message = "Product updated."
    extra_context = {"title": "Edit product", "back_url": LIST_URL, "product_form": True}


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
        name = str(self.object)
        try:
            response = super().form_valid(form)
        except ProtectedError:
            messages.warning(
                self.request,
                f'"{name}" has stock history, so it cannot be deleted. '
                'Untick "Active" on its edit page instead.',
            )
            return redirect(self.success_url)
        messages.success(self.request, f'"{name}" deleted.')
        return response


class ProductDelete(DeletePage, DeleteView):
    model = Product
    extra_context = {
        "title": "Delete product", "back_url": LIST_URL,
        "warning": "Products that already have stock history cannot be deleted. Mark them inactive instead.",
    }


class CategoryDelete(DeletePage, DeleteView):
    model = Category
    extra_context = {
        "title": "Delete group",
        "back_url": LIST_URL,
        "warning": "Products in this group are kept and move to \"No group\".",
    }


# =====================  STOCK  =====================
@login_required
def stock_list(request):
    q = request.GET.get("q", "").strip()
    group = request.GET.get("group", "")
    flt = request.GET.get("filter", "")
    can_edit = request.user.is_admin_role

    qs = with_stock(Product.objects.select_related("category").filter(is_active=True))
    if flt == "low":
        qs = low_stock(qs)
    if group.isdigit():
        qs = qs.filter(category_id=group)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(barcode__icontains=q))

    paginator = Paginator(qs.order_by("name"), 50)
    page = paginator.get_page(request.GET.get("page"))

    rows = []
    for p in page:
        kind, label = stock_status(p)
        rows.append({
            "pk": p.pk,
            "code": p.code or "",
            "name": p.name,
            "group": p.category.name if p.category else "",
            "unit": p.get_unit_display(),
            "stock": qty_fmt(p.stock) if p.track_stock else "\u2014",
            "min_stock": p.min_stock if p.track_stock else "\u2014",
            "kind": kind,
            "label": label,
            "value": f"{p.cost_price * p.stock:,.2f}" if p.track_stock else "\u2014",
        })

    params = request.GET.copy()
    params.pop("page", None)

    return render(request, "inventory/stock_list.html", {
        "rows": rows, "page": page, "total": paginator.count,
        "q": q, "group": group, "flt": flt,
        "categories": Category.objects.all(),
        "querystring": params.urlencode(),
        "can_edit": can_edit,
    })


@login_required
def stock_export(request):
    if not request.user.is_admin_role:
        return HttpResponse(status=403)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="stock.csv"'
    writer = csv.writer(response)
    writer.writerow(["Code", "Name", "Group", "Unit", "Stock", "Low stock level", "Cost", "Stock value"])
    for p in with_stock(Product.objects.select_related("category").filter(is_active=True, track_stock=True)).order_by("name"):
        writer.writerow([
            _safe(p.code), _safe(p.name), _safe(p.category.name if p.category else ""),
            p.unit, qty_fmt(p.stock), p.min_stock, p.cost_price, p.cost_price * p.stock,
        ])
    return response


class StockEntry(AdminRequiredMixin, FormView):
    """Add stock, record a count, or record damage/loss. Always writes a new ledger row."""
    template_name = "inventory/form.html"
    success_url = STOCK_URL
    kind = None
    form_classes = {"add": AddStockForm, "count": CountForm, "damage": DamageForm}
    titles = {"add": "Add stock", "count": "Stock count (adjust)", "damage": "Damage / loss"}

    def get_form_class(self):
        return self.form_classes[self.kind]

    def get_initial(self):
        initial = super().get_initial()
        pk = self.request.GET.get("product", "")
        if pk.isdigit():
            initial["product"] = pk
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(title=self.titles[self.kind], back_url=self.success_url)
        return ctx

    def form_valid(self, form):
        cd = form.cleaned_data
        user_note = cd.get("note", "")

        with transaction.atomic():
            # Lock the product row so two people can't change its stock at the same moment
            product = Product.objects.select_for_update().get(pk=cd["product"].pk)
            current = current_stock(product)

            if self.kind == "add":
                qty = cd["quantity"]
                if product.movements.exists():
                    mtype, default_note = StockMovement.Type.ADJUSTMENT, "Stock added"
                else:
                    mtype, default_note = StockMovement.Type.OPENING, "Opening stock"
                note = user_note or default_note
            elif self.kind == "count":
                qty = cd["counted"] - current
                if qty == 0:
                    messages.info(self.request, f"{product.name}: count matches the system ({qty_fmt(current)}). No change made.")
                    return redirect(self.success_url)
                mtype = StockMovement.Type.ADJUSTMENT
                note = f"Count: system {qty_fmt(current)}, counted {qty_fmt(cd['counted'])}"
                if user_note:
                    note += f" \u2014 {user_note}"
            else:  # damage
                qty = -cd["quantity"]
                mtype = StockMovement.Type.DAMAGE
                note = user_note

            StockMovement.objects.create(
                product=product, quantity=qty, movement_type=mtype,
                note=note, created_by=self.request.user,
            )

        messages.success(
            self.request,
            f"{product.name}: stock is now {qty_fmt(current + qty)} (was {qty_fmt(current)}).",
        )
        return super().form_valid(form)


@login_required
def movement_list(request):
    if not request.user.is_admin_role:
        return HttpResponse(status=403)

    product = request.GET.get("product", "")
    mtype = request.GET.get("type", "")

    qs = StockMovement.objects.select_related("product", "created_by")
    selected = None
    if product.isdigit():
        selected = Product.objects.filter(pk=product).first()
        if selected:
            qs = qs.filter(product=selected)
    if mtype in StockMovement.Type.values:
        qs = qs.filter(movement_type=mtype)

    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get("page"))

    params = request.GET.copy()
    params.pop("page", None)

    return render(request, "inventory/movement_list.html", {
        "page": page, "total": paginator.count, "selected": selected,
        "mtype": mtype, "types": StockMovement.Type.choices,
        "querystring": params.urlencode(),
    })