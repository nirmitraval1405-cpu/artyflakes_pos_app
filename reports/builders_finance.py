from decimal import Decimal

from django.db.models import Sum

from dashboard.views import _metrics
from expenses.models import Expense
from pos.models import Payment, Sale, SaleReturn
from purchase.models import PurchasePayment

from .builders_sales import _local_date
from .helpers import Col, Report
from .registry import register

ZERO = Decimal("0")


@register("Finance", "Transaction history", uses=("party", "user"))
def transaction_history(p):
    records = []
    sale_payments = (
        Payment.objects.select_related("sale__customer", "sale__cashier")
        .filter(
            sale__status=Sale.Status.COMPLETED,
            created_at__date__gte=p.date_from,
            created_at__date__lte=p.date_to,
        )
    )
    if p.party:
        sale_payments = sale_payments.filter(sale__customer_id=p.party)
    if p.user:
        sale_payments = sale_payments.filter(sale__cashier_id=p.user)
    for payment in sale_payments:
        records.append({
            "when": _local_date(payment.created_at),
            "type": "Sale payment",
            "reference": payment.sale.invoice_no,
            "party": payment.sale.customer.name,
            "method": payment.get_method_display(),
            "inflow": payment.amount,
            "outflow": ZERO,
            "by": payment.sale.cashier.get_full_name() or payment.sale.cashier.username,
        })

    refunds = SaleReturn.objects.select_related("sale__customer", "processed_by").filter(
        created_at__date__gte=p.date_from,
        created_at__date__lte=p.date_to,
    )
    if p.party:
        refunds = refunds.filter(sale__customer_id=p.party)
    if p.user:
        refunds = refunds.filter(processed_by_id=p.user)
    for refund in refunds:
        records.append({
            "when": _local_date(refund.created_at),
            "type": "Sales refund",
            "reference": refund.return_no,
            "party": refund.sale.customer.name,
            "method": refund.get_refund_method_display(),
            "inflow": ZERO,
            "outflow": refund.refund_total,
            "by": refund.processed_by.get_full_name() or refund.processed_by.username,
        })

    supplier_payments = PurchasePayment.objects.select_related(
        "purchase__supplier", "recorded_by"
    ).filter(payment_date__gte=p.date_from, payment_date__lte=p.date_to)
    if p.party:
        supplier_payments = supplier_payments.filter(purchase__supplier_id=p.party)
    if p.user:
        supplier_payments = supplier_payments.filter(recorded_by_id=p.user)
    for payment in supplier_payments:
        records.append({
            "when": payment.payment_date,
            "type": "Supplier payment",
            "reference": payment.purchase.purchase_no,
            "party": payment.purchase.supplier.name,
            "method": payment.get_method_display(),
            "inflow": ZERO,
            "outflow": payment.amount,
            "by": payment.recorded_by.get_full_name() or payment.recorded_by.username,
        })

    expenses = Expense.objects.select_related("category", "created_by").filter(
        status=Expense.Status.ACTIVE,
        expense_date__gte=p.date_from,
        expense_date__lte=p.date_to,
    )
    if p.user:
        expenses = expenses.filter(created_by_id=p.user)
    for expense in expenses:
        records.append({
            "when": expense.expense_date,
            "type": f"Expense - {expense.category.name}",
            "reference": expense.expense_no,
            "party": expense.payee,
            "method": expense.get_payment_method_display(),
            "inflow": ZERO,
            "outflow": expense.amount,
            "by": expense.created_by.get_full_name() or expense.created_by.username,
        })

    records.sort(key=lambda row: (row["when"], row["type"], row["reference"]))
    cols = [
        Col("Date", "when", "date"), Col("Type", "type"), Col("Reference", "reference"),
        Col("Customer / supplier / payee", "party"), Col("Method", "method"),
        Col("Money in", "inflow", "money"), Col("Money out", "outflow", "money"), Col("Recorded by", "by"),
    ]
    notes = [
        "Includes recorded sale payments, sales refunds, supplier payments, and active expenses. Purchases on account are not cash transactions.",
        "The party filter applies to sales, refunds, and supplier payments. Expenses have no linked contact and remain included.",
    ]
    return Report(
        cols,
        records,
        p.chips(("party", "user"), party_label="Customer or supplier"),
        totals=("inflow", "outflow"),
        notes=notes,
    )


@register("Finance", "Profit and loss")
def profit_and_loss(p):
    metrics = _metrics(p.date_from, p.date_to)
    sales = Sale.objects.filter(
        status=Sale.Status.COMPLETED,
        created_at__date__gte=p.date_from,
        created_at__date__lte=p.date_to,
    ).aggregate(tax=Sum("tax_total"))
    returns = SaleReturn.objects.filter(
        created_at__date__gte=p.date_from,
        created_at__date__lte=p.date_to,
    ).aggregate(tax=Sum("tax_total"))
    sales_tax = sales["tax"] or ZERO
    return_tax = returns["tax"] or ZERO
    net_revenue = metrics["sales"] - sales_tax - metrics["returns"] + return_tax
    expenses = Expense.objects.filter(
        status=Expense.Status.ACTIVE,
        expense_date__gte=p.date_from,
        expense_date__lte=p.date_to,
    ).aggregate(total=Sum("amount"))["total"] or ZERO
    gross_profit = metrics["profit"]
    cost_of_goods_sold = net_revenue - gross_profit
    records = [
        {"item": "Net sales (excluding GST and returns)", "amount": net_revenue},
        {"item": "Cost of goods sold (net of returned-item cost)", "amount": cost_of_goods_sold},
        {"item": "Gross profit after returns", "amount": gross_profit},
        {"item": "Operating expenses", "amount": expenses},
        {"item": "Net profit", "amount": gross_profit - expenses},
    ]
    cols = [Col("Item", "item"), Col("Amount", "amount", "money")]
    notes = [
        "Gross profit follows the dashboard calculation: sales excluding GST, less product cost and returned-item cost.",
        "Net profit is gross profit after returns minus active expenses. Purchases are not separately deducted because product costs are counted when sold.",
    ]
    return Report(cols, records, [f"Dates: {p.date_from:%d-%m-%Y} to {p.date_to:%d-%m-%Y}"], notes=notes)
