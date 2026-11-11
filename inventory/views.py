import csv
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, ProtectedError, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, FormView, ListView, UpdateView, View

from core.permissions import AdminRequiredMixin

from .forms import (
    AddStockForm, BrandForm, CategoryForm, CountForm, DamageForm, ProductForm, UnitForm,
)
from .models import Brand, Category, Product, StockMovement, Unit
from .product_import import IMPORT_FIELDS, import_product_rows, parse_product_csv
from .stock import current_stock, low_stock, qty_fmt, stock_status, with_stock
from .utils.barcode_generator import create_barcode_image, generate_barcode

LIST_URL = reverse_lazy("inventory:product_list")
STOCK_URL = reverse_lazy("inventory:stock_list")


def _safe(value):
    """Stop spreadsheet apps from running a cell that starts with = + - @"""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _ordered_category_tree(categories):
    categories = list(categories)
    children = {}
    for category in categories:
        children.setdefault(category.parent_id, []).append(category)

    ordered = []
    visited = set()

    def append_children(parent_id, depth):
        for category in children.get(parent_id, []):
            if category.pk in visited:
                continue
            visited.add(category.pk)
            category.category_depth = depth
            ordered.append(category)
            append_children(category.pk, depth + 1)

    append_children(None, 0)
    for category in categories:
        if category.pk not in visited:
            category.category_depth = 0
            ordered.append(category)
            append_children(category.pk, 1)
    return ordered


def _nested_category_tree(categories, selected_category=None):
    categories = list(categories)
    by_id = {category.pk: category for category in categories}
    children = {}
    for category in categories:
        category.tree_children = []
        children.setdefault(category.parent_id, []).append(category)

    for category in categories:
        category.tree_children = children.get(category.pk, [])

    open_ids = set()
    current = selected_category
    while current:
        open_ids.add(current.pk)
        current = by_id.get(current.parent_id)
    for category in categories:
        category.tree_open = category.pk in open_ids

    return children.get(None, [])


# =====================  PRODUCTS  =====================
@login_required
def product_list(request):
    q = request.GET.get("q", "").strip()
    group = request.GET.get("group", "")

    products = with_stock(Product.objects.select_related("category", "brand"))
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
    categories = list(Category.objects.annotate(n=Count("products")))

    context = {
        "page": page,
        "total": paginator.count,
        "q": q,
        "group": group,
        "selected_category": selected_category,
        "categories": _ordered_category_tree(categories),
        "category_tree": _nested_category_tree(categories, selected_category),
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
    writer.writerow(["Code", "Name", "Group", "Brand", "Barcode", "Unit", "Cost", "Sale price", "MRP", "GST %", "HSN", "Stock", "Low stock level", "Active"])
    for p in with_stock(Product.objects.select_related("category", "brand")).order_by("name"):
        writer.writerow([
            _safe(p.code), _safe(p.name), _safe(p.category.name if p.category else ""),
            _safe(p.brand.name if p.brand else ""), _safe(p.barcode), p.unit,
            p.cost_price, p.sale_price, p.mrp, p.gst_rate,
            _safe(p.hsn_code), qty_fmt(p.stock) if p.track_stock else "", p.min_stock,
            "Yes" if p.is_active else "No",
        ])
    return response


@login_required
def product_import(request):
    if not request.user.is_admin_role:
        raise PermissionDenied

    session_key = "inventory_product_import"
    if request.method == "GET" and request.GET.get("new") == "1":
        request.session.pop(session_key, None)
    stage = request.session.get(session_key)
    if request.method == "GET":
        return render(request, "inventory/product_import.html", {
            "stage": stage,
            "fields": IMPORT_FIELDS,
        })

    action = request.POST.get("action")
    if action == "upload":
        upload = request.FILES.get("file")
        if upload is None:
            messages.error(request, "Choose a CSV file to import.")
            return redirect("inventory:product_import")
        if not upload.name.lower().endswith(".csv"):
            messages.error(request, "Upload a .csv file.")
            return redirect("inventory:product_import")
        content = upload.read(5 * 1024 * 1024 + 1)
        if len(content) > 5 * 1024 * 1024:
            messages.error(request, "CSV files must be 5 MB or smaller.")
            return redirect("inventory:product_import")
        try:
            text = content.decode("utf-8-sig")
            headers, rows, mapping = parse_product_csv(text)
        except (UnicodeDecodeError, ValueError, csv.Error) as exc:
            messages.error(request, str(exc))
            return redirect("inventory:product_import")
        stage = {
            "token": secrets.token_urlsafe(18),
            "headers": headers,
            "rows": rows,
            "mapping": {str(index): value for index, value in mapping.items()},
            "samples": rows[:3],
            "matching_behavior": "update",
            "columns": [
                {
                    "index": index,
                    "header": header or f"Column {index + 1}",
                    "samples": [row[index] if index < len(row) else "" for row in rows[:3]],
                    "mapped": mapping.get(index, ""),
                }
                for index, header in enumerate(headers)
            ],
        }
        request.session[session_key] = stage
        return render(request, "inventory/product_import.html", {
            "stage": stage,
            "fields": IMPORT_FIELDS,
        })

    if action != "import" or not stage or request.POST.get("token") != stage.get("token"):
        messages.error(request, "Your CSV import session expired. Upload the file again.")
        return redirect("inventory:product_import")

    allowed_fields = {key for key, _ in IMPORT_FIELDS}
    mapping = {}
    for index in range(len(stage["headers"])):
        target = request.POST.get(f"map_{index}", "")
        if target and target not in allowed_fields:
            messages.error(request, "The selected column mapping is invalid.")
            return redirect("inventory:product_import")
        if target:
            mapping[index] = target

    try:
        result = import_product_rows(
            stage["rows"], mapping, request.POST.get("matching_behavior", ""), request.user
        )
    except ValueError as exc:
        stage["mapping"] = {str(index): value for index, value in mapping.items()}
        stage["matching_behavior"] = request.POST.get("matching_behavior", "update")
        for column in stage["columns"]:
            column["mapped"] = mapping.get(column["index"], "")
        request.session[session_key] = stage
        return render(request, "inventory/product_import.html", {
            "stage": stage,
            "fields": IMPORT_FIELDS,
            "import_error": str(exc),
        })

    del request.session[session_key]
    return render(request, "inventory/product_import.html", {
        "result": result,
        "fields": IMPORT_FIELDS,
    })


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
    success_url = reverse_lazy("inventory:category_list")
    saved_message = "Category added."
    extra_context = {"title": "New category", "back_url": reverse_lazy("inventory:category_list")}

    def get_initial(self):
        initial = super().get_initial()
        parent_id = self.request.GET.get("parent", "")
        if parent_id.isdigit():
            parent = Category.objects.filter(pk=parent_id).first()
            if parent:
                initial["parent"] = parent
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if context["form"]["parent"].value():
            context["title"] = "New subcategory"
        return context


class CategoryUpdate(FormPage, UpdateView):
    model = Category
    form_class = CategoryForm
    success_url = reverse_lazy("inventory:category_list")
    saved_message = "Category updated."
    extra_context = {"title": "Edit category", "back_url": reverse_lazy("inventory:category_list")}


class CatalogList(AdminRequiredMixin, ListView):
    template_name = "inventory/catalog_list.html"
    context_object_name = "items"
    kind = ""

    def get_queryset(self):
        q = (self.request.GET.get("q") or "").strip()

        if self.kind == "category":
            qs = Category.objects.annotate(product_count=Count("products"))
            if q:
                qs = qs.filter(name__icontains=q)
            return qs
        if self.kind == "brand":
            qs = Brand.objects.annotate(product_count=Count("products"))
            if q:
                qs = qs.filter(name__icontains=q)
            return qs

        counts = dict(
            Product.objects.values("unit").annotate(n=Count("pk")).values_list("unit", "n")
        )
        units = list(Unit.objects.all())
        for unit in units:
            unit.product_count = counts.get(unit.code, 0)
        if q:
            units = [u for u in units if q.lower() in u.name.lower() or q.lower() in u.code.lower()]
        return units

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        labels = {"category": "Categories", "brand": "Brands", "unit": "Units"}
        context.update({
            "kind": self.kind,
            "title": labels[self.kind],
            "q": (self.request.GET.get("q") or "").strip(),
        })
        return context


class CategoryList(CatalogList):
    kind = "category"

    def get_queryset(self):
        q = (self.request.GET.get("q") or "").strip()
        categories = Category.objects.annotate(
            product_count=Count("products"),
            child_count=Count("children"),
        )
        if q:
            categories = categories.filter(name__icontains=q)
        return _ordered_category_tree(categories)


class BrandList(CatalogList):
    kind = "brand"


class UnitList(CatalogList):
    kind = "unit"


class BrandCreate(FormPage, CreateView):
    model = Brand
    form_class = BrandForm
    success_url = reverse_lazy("inventory:brand_list")
    saved_message = "Brand added."
    extra_context = {"title": "New brand", "back_url": reverse_lazy("inventory:brand_list")}


class BrandUpdate(FormPage, UpdateView):
    model = Brand
    form_class = BrandForm
    success_url = reverse_lazy("inventory:brand_list")
    saved_message = "Brand updated."
    extra_context = {"title": "Edit brand", "back_url": reverse_lazy("inventory:brand_list")}


class UnitCreate(FormPage, CreateView):
    model = Unit
    form_class = UnitForm
    success_url = reverse_lazy("inventory:unit_list")
    saved_message = "Unit added."
    extra_context = {"title": "New unit", "back_url": reverse_lazy("inventory:unit_list")}


class UnitUpdate(FormPage, UpdateView):
    model = Unit
    form_class = UnitForm
    success_url = reverse_lazy("inventory:unit_list")
    saved_message = "Unit updated."
    extra_context = {"title": "Edit unit", "back_url": reverse_lazy("inventory:unit_list")}


class CatalogToggle(AdminRequiredMixin, View):
    model = None
    list_url = None

    def post(self, request, pk):
        item = get_object_or_404(self.model, pk=pk)
        item.is_active = not item.is_active
        item.save(update_fields=["is_active", "updated_at"])
        state = "activated" if item.is_active else "deactivated"
        messages.success(request, f"{item} {state}.")
        return redirect(self.list_url)


class CategoryToggle(CatalogToggle):
    model = Category
    list_url = reverse_lazy("inventory:category_list")


class BrandToggle(CatalogToggle):
    model = Brand
    list_url = reverse_lazy("inventory:brand_list")


class UnitToggle(CatalogToggle):
    model = Unit
    list_url = reverse_lazy("inventory:unit_list")


class CatalogDelete(AdminRequiredMixin, DeleteView):
    template_name = "inventory/confirm_delete.html"
    model = None
    kind = ""
    list_url = None
    product_filter = None

    def get_success_url(self):
        return self.list_url

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        labels = {"category": "category", "brand": "brand", "unit": "unit"}
        context.update({
            "title": f"Delete {labels[self.kind]}",
            "back_url": self.list_url,
            "warning": "This can only be deleted if no products use it. Otherwise deactivate it instead.",
        })
        return context

    def form_valid(self, form):
        if self.product_filter(self.object).exists():
            messages.warning(
                self.request,
                f'"{self.object}" is used by products and cannot be deleted. Deactivate it instead.',
            )
            return redirect(self.list_url)
        name = str(self.object)
        self.object.delete()
        messages.success(self.request, f'"{name}" deleted.')
        return redirect(self.list_url)


class CategoryDelete(CatalogDelete):
    model = Category
    kind = "category"
    list_url = reverse_lazy("inventory:category_list")
    product_filter = staticmethod(lambda category: Product.objects.filter(category=category))

    def form_valid(self, form):
        if self.object.children.exists():
            messages.warning(
                self.request,
                f'"{self.object}" has subcategories and cannot be deleted. Reassign or delete them first.',
            )
            return redirect(self.list_url)
        return super().form_valid(form)


class BrandDelete(CatalogDelete):
    model = Brand
    kind = "brand"
    list_url = reverse_lazy("inventory:brand_list")
    product_filter = staticmethod(lambda brand: Product.objects.filter(brand=brand))


class UnitDelete(CatalogDelete):
    model = Unit
    kind = "unit"
    list_url = reverse_lazy("inventory:unit_list")
    product_filter = staticmethod(lambda unit: Product.objects.filter(unit=unit.code))


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
        "warning": "Products with stock history or a WooCommerce link cannot be deleted. Mark them inactive instead.",
    }

    def has_history(self, product):
        return (
            product.movements.exists()
            or product.sale_items.exists()
            or hasattr(product, "woo_link")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.has_history(self.object):
            context["title"] = "Archive product"
            context["warning"] = (
                "This product has stock or sales history, or is linked to WooCommerce, so it cannot be "
                "permanently deleted. Confirm to mark it inactive. Its records and website link will be preserved."
            )
            context["confirm_label"] = "Archive product"
        return context

    def form_valid(self, form):
        with transaction.atomic():
            product = Product.objects.select_for_update().get(pk=self.object.pk)
            if self.has_history(product):
                product.is_active = False
                product.save(update_fields=["is_active", "updated_at"])
                messages.success(
                    self.request,
                    f'"{product.name}" was archived and is no longer available for new sales.',
                )
                return redirect(self.success_url)
            self.object = product
            return super().form_valid(form)


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
            "unit": p.unit,
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
        "categories": _ordered_category_tree(Category.objects.all()),
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
@login_required
def price_tags(request):
    products = Product.objects.filter(is_active=True).order_by("name")
    data = [
        {
            "id": p.pk,
            "name": p.name,
            "code": p.code or "",
            "barcode": p.barcode or "",
            "price": str(p.sale_price),
            "mrp": str(p.mrp),
            "cat": p.category_id,
        }
        for p in products
    ]
    # Opened from a group on the Products page: pre-select that group's products
    group = request.GET.get("group", "")
    if group.isdigit():
        pre = [p["id"] for p in data if p["cat"] == int(group)]
    elif group == "none":
        pre = [p["id"] for p in data if p["cat"] is None]
    else:
        pre = []
    return render(request, "inventory/price_tags.html", {"products": data, "preselect": pre})