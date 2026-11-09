from django.db.models import Sum
from pos.models import Sale, SaleItem

from .builders_sales import _local_date
from .helpers import Col, Report
from .registry import register


def _customer_name(customer_name, is_system):
    return "Walk-in customer" if is_system else customer_name


def _sale_items(p):
    items = SaleItem.objects.filter(
        sale__status=Sale.Status.COMPLETED,
        sale__created_at__date__gte=p.date_from,
        sale__created_at__date__lte=p.date_to,
    )
    if p.party:
        items = items.filter(sale__customer_id=p.party)
    if p.user:
        items = items.filter(sale__cashier_id=p.user)
    if p.product:
        items = items.filter(product_id=p.product)
    if p.group:
        items = items.filter(product__category_id=p.group)
    return items


@register("Customers", "Sales by Customer", uses=("party", "user", "product", "group"))
def sales_by_customer(p):
    rows = (
        _sale_items(p)
        .order_by()
        .values("sale__customer_id", "sale__customer__name", "sale__customer__is_system")
        .annotate(
            before_tax=Sum("line_total") - Sum("tax_amount"),
            total=Sum("line_total"),
        )
        .order_by("sale__customer__name", "sale__customer_id")
    )
    records = [
        {
            "customer": _customer_name(row["sale__customer__name"], row["sale__customer__is_system"]),
            "before_tax": row["before_tax"],
            "total": row["total"],
        }
        for row in rows
    ]
    cols = [
        Col("Customer", "customer"), Col("Total before tax", "before_tax", "money"),
        Col("Total", "total", "money"),
    ]
    return Report(
        cols,
        records,
        p.chips(("party", "user", "product", "group")),
        totals=("before_tax", "total"),
    )


@register(
    "Customers",
    "Invoice List",
    uses=("party", "user", "product", "group", "payment_method"),
)
def invoice_list(p):
    sales = Sale.objects.filter(
        status=Sale.Status.COMPLETED,
        created_at__date__gte=p.date_from,
        created_at__date__lte=p.date_to,
    )
    if p.party:
        sales = sales.filter(customer_id=p.party)
    if p.user:
        sales = sales.filter(cashier_id=p.user)
    if p.product:
        sales = sales.filter(items__product_id=p.product)
    if p.group:
        sales = sales.filter(items__product__category_id=p.group)
    if p.payment_method:
        sales = sales.filter(payments__method=p.payment_method)

    sales = sales.distinct().select_related("customer").prefetch_related("payments").order_by("created_at", "id")
    records = []
    for index, sale in enumerate(sales, start=1):
        methods = list(dict.fromkeys(payment.get_method_display() for payment in sale.payments.all()))
        records.append({
            "number": index,
            "date": _local_date(sale.created_at),
            "document": sale.invoice_no,
            "customer": _customer_name(sale.customer.name, sale.customer.is_system),
            "method": ", ".join(methods),
            "tax": sale.tax_total,
            "total": sale.total,
        })
    cols = [
        Col("#", "number", "int"), Col("Date", "date", "date"), Col("Document number", "document"),
        Col("Customer", "customer"), Col("Payment method", "method"), Col("Tax", "tax", "money"),
        Col("Total", "total", "money"),
    ]
    return Report(
        cols,
        records,
        p.chips(("party", "user", "product", "group")),
        totals=("tax", "total"),
    )


@register("Customers", "Item List", uses=("party", "user", "product", "group"))
def customer_item_list(p):
    items = _sale_items(p).select_related("sale__customer", "product").order_by(
        "sale__customer__name", "sale__created_at", "sale_id", "id"
    )
    records = []
    for index, item in enumerate(items, start=1):
        sku = item.product_code or item.product.barcode or ""
        records.append({
            "number": index,
            "customer": _customer_name(item.sale.customer.name, item.sale.customer.is_system),
            "item": f"{item.product_name} / {sku}" if sku else item.product_name,
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "tax": item.tax_amount,
            "total": item.line_total,
        })
    cols = [
        Col("#", "number", "int"), Col("Customer", "customer"), Col("Item / SKU", "item"),
        Col("Qty", "quantity", "qty"), Col("Unit Price", "unit_price", "money"),
        Col("Tax", "tax", "money"), Col("Total", "total", "money"),
    ]
    return Report(
        cols,
        records,
        p.chips(("party", "user", "product", "group")),
        totals=("quantity", "total"),
    )
