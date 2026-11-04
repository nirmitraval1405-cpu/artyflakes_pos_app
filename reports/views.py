import csv

from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from core.permissions import AdminRequiredMixin
from inventory.models import Category, Product

from .registry import REPORTS, SECTIONS


def _safe(value):
    """Stop spreadsheet apps from running a cell that starts with = + - @"""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


class ReportIndex(AdminRequiredMixin, TemplateView):
    template_name = "reports/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        ctx.update(
            sections=SECTIONS,
            products=Product.objects.only("id", "name").order_by("name"),
            categories=Category.objects.all(),
            date_from=today.replace(day=1),
            date_to=today,
        )
        return ctx


# ---------- Report builders ----------
# Each returns: headers, rows (for the page), csv_rows (for download), filter descriptions

def product_list_report(request):
    qs = Product.objects.select_related("category").order_by("name")
    filters = []

    group = request.GET.get("group", "")
    if group.isdigit():
        cat = Category.objects.filter(pk=group).first()
        if cat:
            qs = qs.filter(category=cat)
            filters.append(f"Group: {cat.name}")

    product = request.GET.get("product", "")
    if product.isdigit():
        prod = Product.objects.filter(pk=product).first()
        if prod:
            qs = qs.filter(pk=prod.pk)
            filters.append(f"Product: {prod.name}")

    headers = [
        ("Code", ""), ("Name", ""), ("Group", ""), ("Barcode", ""), ("Unit", ""),
        ("Cost", "num"), ("Sale price", "num"), ("MRP", "num"), ("GST %", "num"), ("Status", ""),
    ]
    rows, csv_rows = [], []
    for p in qs:
        status = "Active" if p.is_active else "Inactive"
        rows.append([
            (p.code or "", ""), (p.name, ""), (p.category.name if p.category else "", ""),
            (p.barcode or "", ""), (p.get_unit_display(), ""),
            (f"{p.cost_price:,.2f}", "num"), (f"{p.sale_price:,.2f}", "num"),
            (f"{p.mrp:,.2f}", "num"), (f"{p.gst_rate:.2f}".rstrip("0").rstrip("."), "num"),
            (status, ""),
        ])
        csv_rows.append([
            _safe(p.code), _safe(p.name), _safe(p.category.name if p.category else ""),
            _safe(p.barcode), p.get_unit_display(),
            p.cost_price, p.sale_price, p.mrp, p.gst_rate, status,
        ])
    return headers, rows, csv_rows, filters


BUILDERS = {
    "inventory-product-list": product_list_report,
}


class ReportView(AdminRequiredMixin, View):
    def get(self, request, slug):
        report = REPORTS.get(slug)
        builder = BUILDERS.get(slug)
        if not report or not report["ready"] or not builder:
            raise Http404("This report is not available yet.")

        headers, rows, csv_rows, filters = builder(request)

        if request.GET.get("format") == "csv":
            response = HttpResponse(content_type="text/csv")
            response["Content-Disposition"] = f'attachment; filename="{slug}.csv"'
            writer = csv.writer(response)
            writer.writerow([h[0] for h in headers])
            writer.writerows(csv_rows)
            return response

        return render(request, "reports/report.html", {
            "report": report,
            "headers": headers,
            "rows": rows,
            "filters": filters,
            "generated": timezone.localtime(),
            "autoprint": request.GET.get("print") == "1",
        })