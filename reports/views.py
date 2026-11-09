import csv
import calendar
from datetime import date, timedelta
from . import gstr3b
from django.shortcuts import redirect
from django.urls import reverse

from . import gstr1
from django.contrib.auth import get_user_model
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from core.permissions import AdminRequiredMixin
from inventory.models import Category, Product
from people.models import Contact

from . import builders_purchase, builders_sales, builders_stock  # noqa: F401  (importing them registers the reports)
from . import helpers
from .helpers import FILTER_LABELS, Params
from .registry import BUILDERS, USES, get_sections


class ReportIndex(AdminRequiredMixin, TemplateView):
    template_name = "reports/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        p = Params(self.request)
        contacts = Contact.objects.filter(is_active=True, is_system=False).order_by("name")
        ctx.update(
            sections=get_sections()[0],
            products=Product.objects.only("id", "name").order_by("name"),
            categories=Category.objects.all(),
            customers=contacts.filter(is_customer=True),
            suppliers=contacts.filter(is_supplier=True),
            users=get_user_model().objects.order_by("username"),
            date_from=p.date_from,
            date_to=p.date_to,
        )
        return ctx


class ReportView(AdminRequiredMixin, View):
    def get(self, request, slug):
        _, reports = get_sections()
        info, builder = reports.get(slug), BUILDERS.get(slug)
        if not info or not builder:
            raise Http404("This report is not available yet.")

        p = Params(request)
        report = builder(p)

        notes = list(p.notes) + list(report.notes)
        for name in sorted(p.supplied - USES.get(slug, set())):
            notes.append(f"The {FILTER_LABELS[name]} filter does not apply to this report, so it was ignored.")

        if request.GET.get("format") == "csv":
            return self._csv(slug, report)

        rows, truncated = report.page_rows()
        return render(request, "reports/report.html", {
            "report": report,
            "info": info,
            "rows": rows,
            "total_row": report.page_total_row(),
            "truncated": truncated,
            "total_rows": len(report.records),
            "page_cap": helpers.PAGE_CAP,
            "csv_cap": helpers.CSV_CAP,
            "notes": notes,
            "generated": timezone.localtime(),
            "autoprint": request.GET.get("print") == "1",
        })

    def _csv(self, slug, report):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{slug}.csv"'
        writer = csv.writer(response)
        writer.writerow([c.label for c in report.cols])
        rows, truncated = report.csv_rows()
        writer.writerows(rows)
        total = report.csv_total_row()
        if total:
            writer.writerow(total)
        if truncated:
            writer.writerow([f"Only the first {helpers.CSV_CAP} rows are included. Narrow the dates."])
        return response
def _gstr1_month(text):
    """First day of the month asked for (YYYY-MM). Default: last month, which is what you file."""
    try:
        year, month = (int(p) for p in str(text).split("-"))
        return date(year, month, 1)
    except (ValueError, TypeError):
        return (timezone.localdate().replace(day=1) - timedelta(days=1)).replace(day=1)


def _gstr1_data(first):
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    shop_gstin, sales, returns = gstr1.collect(first, last)
    if not shop_gstin:
        raise ValueError("Add your GSTIN in Store Settings first. GSTR-1 needs it.")
    return gstr1.build_sections(sales, returns, shop_gstin)


class Gstr1Page(AdminRequiredMixin, View):
    def get(self, request):
        first = _gstr1_month(request.GET.get("month"))
        ctx = {"month": f"{first:%Y-%m}", "label": f"{first:%B %Y}"}
        try:
            ctx.update(_gstr1_data(first))
        except ValueError as exc:
            ctx["error"] = str(exc)
        return render(request, "reports/gstr1.html", ctx)


class Gstr1Download(AdminRequiredMixin, View):
    def get(self, request):
        first = _gstr1_month(request.GET.get("month"))
        try:
            data = _gstr1_data(first)
        except ValueError:
            return redirect(f"{reverse('reports:gstr1')}?month={first:%Y-%m}")   # the page shows the reason
        response = HttpResponse(
            gstr1.build_workbook(data["sheets"]),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="GSTR1_{first:%m%Y}.xlsx"'
        return response
    
def _gstr3b_data(first):
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    shop_gstin, sales, returns = gstr1.collect(first, last)
    if not shop_gstin:
        raise ValueError("Add your GSTIN in Store Settings first. GSTR-3B needs it.")
    purchases = gstr3b.collect_purchases(first, last)
    return shop_gstin, gstr3b.build_3b(sales, returns, purchases, shop_gstin)


class Gstr3bPage(AdminRequiredMixin, View):
    def get(self, request):
        first = _gstr1_month(request.GET.get("month"))
        ctx = {"month": f"{first:%Y-%m}", "label": f"{first:%B %Y}"}
        try:
            _, data = _gstr3b_data(first)
            ctx.update(t=data["totals"], warnings=data["warnings"])
        except ValueError as exc:
            ctx["error"] = str(exc)
        return render(request, "reports/gstr3b.html", ctx)


class Gstr3bDownload(AdminRequiredMixin, View):
    def get(self, request):
        from storeconfig.models import StoreSettings

        first = _gstr1_month(request.GET.get("month"))
        try:
            gstin, data = _gstr3b_data(first)
        except ValueError:
            return redirect(f"{reverse('reports:gstr3b')}?month={first:%Y-%m}")
        xlsx = gstr3b.build_workbook(data, f"{first:%B %Y}", gstin, StoreSettings.load().name)
        response = HttpResponse(
            xlsx, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = f'attachment; filename="GSTR3B_{first:%m%Y}.xlsx"'
        return response