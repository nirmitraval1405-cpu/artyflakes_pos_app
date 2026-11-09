from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import DeleteView, TemplateView

from core.permissions import AdminRequiredMixin
from inventory.models import Brand, Category, Product

from .forms import PromotionForm
from .models import Promotion
from .services import PromotionError, parse_items, save_promotion

LIST_URL = reverse_lazy("promotions:list")
BLANK_ROW = {
    "target_type": "product", "target_id": "", "product": "",
    "kind": "percent", "value": "", "min_qty": "",
}
BADGE = {"live": "st-ok", "scheduled": "st-low", "idle": "st-low", "expired": "st-na", "off": "st-na"}


def _period(p):
    def fmt(dt):
        return timezone.localtime(dt).strftime("%d-%m-%Y %H:%M")
    if p.starts_at and p.ends_at:
        return f"{fmt(p.starts_at)} to {fmt(p.ends_at)}"
    if p.starts_at:
        return f"From {fmt(p.starts_at)}"
    if p.ends_at:
        return f"Until {fmt(p.ends_at)}"
    return "No time limit"


def _rows_from_items(promotion):
    return [
        {
            "target_type": "product" if i.product_id else "category" if i.category_id else "brand",
            "target_id": str(i.product_id or i.category_id or i.brand_id),
            "product": str(i.product_id or ""),
            "kind": i.kind, "value": f"{i.value:.2f}",
            "min_qty": f"{i.min_qty.normalize():f}" if i.min_qty else "",
        }
        for i in promotion.items.all()
    ] or [dict(BLANK_ROW)]


def _rows_from_post(post):
    """Gives the typed rows back after an error, so nothing has to be retyped."""
    target_types = post.getlist("target_type")
    target_ids = post.getlist("target_id")
    if not target_types:
        target_types = ["product"] * len(post.getlist("product"))
        target_ids = post.getlist("product")
    rows = []
    for i, target_id in enumerate(target_ids):
        target_type = target_types[i] if i < len(target_types) else "product"
        target_id = (target_id or "").strip()
        rows.append({
            "target_type": target_type, "target_id": target_id,
            "product": target_id if target_type == "product" else "",
            "kind": post.get(f"kind_{i}", "percent"),
            "value": post.get(f"value_{i}", ""), "min_qty": post.get(f"min_qty_{i}", ""),
        })
    return rows or [dict(BLANK_ROW)]


class PromotionList(AdminRequiredMixin, TemplateView):
    template_name = "promotions/list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()
        paginator = Paginator(
            Promotion.objects.prefetch_related("items__product", "items__category", "items__brand"), 50
        )
        page = paginator.get_page(self.request.GET.get("page"))

        rows = []
        for p in page:
            key, label = p.state(now)
            names = list(dict.fromkeys(
                f"Product: {i.product.name}" if i.product_id else
                f"Category: {i.category.name}" if i.category_id else
                f"Brand: {i.brand.name}"
                for i in p.items.all()
            ))
            more = len(names) - 3
            rows.append({
                "p": p, "state_label": label, "badge": BADGE[key], "period": _period(p),
                "products": ", ".join(names[:3]) + (f" +{more} more" if more > 0 else ""),
                "count": len(names),
            })
        ctx.update(rows=rows, page=page, total=paginator.count)
        return ctx


class PromotionEdit(AdminRequiredMixin, View):
    template_name = "promotions/form.html"

    def _render(self, request, form, rows, promotion):
        return render(request, self.template_name, {
            "form": form, "rows": rows, "promotion": promotion,
            "title": "Edit promotion" if promotion else "New promotion",
            "products": Product.objects.filter(is_active=True).order_by("name"),
            "categories": Category.objects.filter(is_active=True).order_by("name"),
            "brands": Brand.objects.filter(is_active=True).order_by("name"),
        })

    def get(self, request, pk=None):
        promotion = get_object_or_404(Promotion, pk=pk) if pk else None
        rows = _rows_from_items(promotion) if promotion else [dict(BLANK_ROW)]
        return self._render(request, PromotionForm(instance=promotion), rows, promotion)

    def post(self, request, pk=None):
        promotion = get_object_or_404(Promotion, pk=pk) if pk else None
        form = PromotionForm(request.POST, instance=promotion)
        rows = _rows_from_post(request.POST)
        if not form.is_valid():
            return self._render(request, form, rows, promotion)

        try:
            items = parse_items(request.POST)
        except PromotionError as exc:
            form.add_error(None, str(exc))
            return self._render(request, form, rows, promotion)

        promo = form.save(commit=False)
        promo.weekdays = form.weekdays_value()
        if promo.created_by_id is None:
            promo.created_by = request.user
        save_promotion(promo, items)
        messages.success(request, f'Promotion "{promo.name}" saved. It applies on the POS from the next bill.')
        return redirect("promotions:list")


class PromotionDelete(AdminRequiredMixin, DeleteView):
    model = Promotion
    template_name = "inventory/confirm_delete.html"       # the shared confirm page
    success_url = LIST_URL
    extra_context = {
        "title": "Delete promotion", "back_url": LIST_URL,
        "warning": "Bills already made keep the discount they were given.",
    }

    def form_valid(self, form):
        messages.success(self.request, f'"{self.object}" deleted.')
        return super().form_valid(form)


@require_POST
def promotion_toggle(request, pk):
    if not (request.user.is_authenticated and request.user.is_admin_role):
        return redirect("pos:screen")
    promo = get_object_or_404(Promotion, pk=pk)
    promo.is_active = not promo.is_active
    promo.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f'"{promo.name}" is now {"on" if promo.is_active else "off"}.')
    return redirect("promotions:list")