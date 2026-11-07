import csv
import uuid
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import F, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView, View

from core.permissions import AdminRequiredMixin
from inventory.models import Product
from people.models import Contact

from .forms import PurchaseHeaderForm
from .models import Purchase, PurchasePayment
from .services import PurchaseError, add_payment, cancel_purchase, create_purchase, parse_date, parse_lines

LIST_URL = reverse_lazy("purchase:list")


def _safe(value):
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _date_or_none(text):
    try:
        return parse_date(text)
    except PurchaseError:
        return None


@login_required
def purchase_list(request):
    if not request.user.is_admin_role:
        return redirect("pos:screen")

    q = request.GET.get("q", "").strip()
    supplier_id = request.GET.get("supplier", "")
    status = request.GET.get("status", "")
    from_date = request.GET.get("from_date", "")
    to_date = request.GET.get("to_date", "")

    qs = Purchase.objects.select_related("supplier", "created_by")
    if q:
        qs = qs.filter(
            Q(purchase_no__icontains=q) | Q(supplier_invoice_no__icontains=q) | Q(supplier__name__icontains=q)
        )
    if supplier_id.isdigit():
        qs = qs.filter(supplier_id=supplier_id)
    else:
        supplier_id = ""

    live = Purchase.Status.COMPLETED
    if status == "cancelled":
        qs = qs.filter(status=Purchase.Status.CANCELLED)
    elif status == "paid":
        qs = qs.filter(status=live, amount_paid__gte=F("grand_total"))
    elif status == "partial":
        qs = qs.filter(status=live, amount_paid__gt=0, amount_paid__lt=F("grand_total"))
    elif status == "unpaid":
        qs = qs.filter(status=live, amount_paid=0)

    if _date_or_none(from_date):
        qs = qs.filter(purchase_date__gte=_date_or_none(from_date))
    if _date_or_none(to_date):
        qs = qs.filter(purchase_date__lte=_date_or_none(to_date))

    paginator = Paginator(qs.order_by("-purchase_date", "-id"), 25)
    page = paginator.get_page(request.GET.get("page"))

    params = request.GET.copy()
    params.pop("page", None)

    return render(request, "purchase/list.html", {
        "page": page, "q": q, "supplier_id": supplier_id, "status": status,
        "from_date": from_date, "to_date": to_date,
        "suppliers": Contact.objects.filter(is_supplier=True, is_active=True).order_by("name"),
        "total": paginator.count, "querystring": params.urlencode(),
    })


@login_required
def purchase_detail(request, pk):
    if not request.user.is_admin_role:
        return redirect("pos:screen")
    purchase = get_object_or_404(
        Purchase.objects.select_related("supplier", "created_by").prefetch_related("items", "payments"), pk=pk
    )
    return render(request, "purchase/detail.html", {"purchase": purchase})


BLANK_ROW = {"product": "", "qty": "1", "cost": "0", "disc": "0", "gst": "0"}


def _rows_from_post(post):
    """Gives the typed product rows back to the form after an error, so nothing has to be retyped."""
    rows = []
    for i, pid in enumerate(post.getlist("product")):
        rows.append({
            "product": (pid or "").strip(),
            "qty": post.get(f"quantity_{i}", "1"),
            "cost": post.get(f"cost_price_{i}", "0"),
            "disc": post.get(f"discount_{i}", "0"),
            "gst": post.get(f"gst_rate_{i}", "0"),
        })
    return rows or [dict(BLANK_ROW)]


class PurchaseCreate(AdminRequiredMixin, TemplateView):
    template_name = "purchase/form.html"

    def _context(self, form, token, rows):
        return {
            "form": form,
            "products": Product.objects.filter(is_active=True).order_by("name"),
            "title": "New purchase",
            "back_url": LIST_URL,
            "token": token,
            "rows": rows,
        }

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        initial = {"purchase_date": self.request.GET.get("date") or timezone.localdate()}
        ctx.update(self._context(PurchaseHeaderForm(initial=initial), uuid.uuid4().hex, [dict(BLANK_ROW)]))
        return ctx

    def post(self, request, *args, **kwargs):
        token = request.POST.get("client_token") or uuid.uuid4().hex
        rows = _rows_from_post(request.POST)
        form = PurchaseHeaderForm(request.POST)
        if not form.is_valid():
            return self.render_to_response(self._context(form, token, rows))

        cd = form.cleaned_data
        pay_amount = Decimal("0") if cd.get("payment_due") else (cd.get("amount_paid") or Decimal("0"))
        try:
            purchase, duplicate = create_purchase(
                request.user,
                {
                    "supplier": cd["supplier"],
                    "supplier_invoice_no": cd["supplier_invoice_no"],
                    "purchase_date": cd["purchase_date"],
                    "notes": cd["notes"],
                },
                parse_lines(request.POST),
                pay_amount,
                cd.get("payment_method") or PurchasePayment.Method.CASH,
                cd.get("payment_reference") or "",
                token,
            )
        except PurchaseError as exc:
            form.add_error(None, str(exc))
            return self.render_to_response(self._context(form, token, rows))

        if duplicate:
            messages.info(request, f"Purchase {purchase.purchase_no} was already saved.")
        else:
            messages.success(request, f"Purchase {purchase.purchase_no} saved.")
        return redirect("purchase:detail", pk=purchase.pk)
    
class PurchasePaymentCreate(AdminRequiredMixin, View):
    def post(self, request, pk):
        try:
            add_payment(request.user, pk, request.POST)
        except PurchaseError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Payment recorded.")
        return redirect("purchase:detail", pk=pk)


@login_required
@require_POST
def purchase_cancel(request, pk):
    if not request.user.is_admin_role:
        return redirect("pos:screen")
    try:
        purchase, warnings = cancel_purchase(request.user, pk, request.POST.get("reason", ""))
    except PurchaseError as exc:
        messages.error(request, str(exc))
        return redirect("purchase:detail", pk=pk)

    messages.success(request, f"Purchase {purchase.purchase_no} was cancelled and its stock was reversed.")
    for text in warnings:
        messages.warning(request, text)
    return redirect("purchase:detail", pk=pk)


@login_required
def purchase_export(request):
    if not request.user.is_admin_role:               # purchase prices are for admins only
        return redirect("pos:screen")

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="purchases.csv"'
    writer = csv.writer(response)
    writer.writerow(["Purchase No", "Supplier", "Supplier invoice", "Date", "Grand total", "Paid", "Outstanding", "Payment", "Status"])
    for p in Purchase.objects.select_related("supplier").order_by("-purchase_date", "-id"):
        writer.writerow([
            _safe(p.purchase_no), _safe(p.supplier.name), _safe(p.supplier_invoice_no), p.purchase_date,
            p.grand_total, p.amount_paid, p.outstanding_amount, p.payment_status, p.get_status_display(),
        ])
    return response