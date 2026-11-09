import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from dashboard.views import _metrics
from expenses.models import Expense, ExpenseCategory
from inventory.models import Category, Product, StockMovement
from people.models import Contact
from pos.models import Payment, Sale
from purchase.models import Purchase, PurchaseItem, PurchasePayment

from .registry import get_sections

Q2 = Decimal("0.01")


class ReportBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("rp_boss", password="pw12345!", role="admin")
        User.objects.create_user("rp_cash", password="pw12345!", role="cashier")
        self.group = Category.objects.create(name="Resin")
        self.product = Product.objects.create(
            name="Resin Clear", category=self.group, sale_price=Decimal("250.00"), mrp=Decimal("300.00"),
            cost_price=Decimal("100.00"), gst_rate=Decimal("18"), min_stock=200,
        )
        StockMovement.objects.create(product=self.product, quantity=100, movement_type="opening")
        self.customer = Contact.objects.create(name="Asha", phone="9876543210", gstin="24ABCDE1234F1Z5")
        self.supplier = Contact.objects.create(name="Resin Co", is_customer=False, is_supplier=True)
        self.today = timezone.localdate()
        self.client.login(username="rp_boss", password="pw12345!")

    def sell(self, qty="2", customer=None, discount=None):
        item = {"product_id": self.product.pk, "quantity": qty}
        if discount:
            item["discount"] = {"type": "percent", "value": discount}
        body = {"items": [item], "payments": [{"method": "cash", "amount": "9999.00"}]}
        if customer:
            body["customer_id"] = customer.pk
        r = self.client.post(reverse("pos:checkout"), data=json.dumps(body), content_type="application/json")
        return Sale.objects.get(invoice_no=r.json()["invoice_no"])

    def refund(self, sale, qty="1", token="r1"):
        item = sale.items.get()
        self.client.post(reverse("pos:sale_return", args=[sale.pk]),
                         {"refund_method": "upi", "client_token": token, f"qty_{item.pk}": qty})

    def report(self, slug, **params):
        return self.client.get(reverse("reports:view", args=[slug]), params)

    def records(self, slug, **params):
        return self.report(slug, **params).context["report"].records

    def purchase(self, total, status="completed", paid="0", day=None, no="AF-P-T-1"):
        pu = Purchase.objects.create(
            purchase_no=no, supplier=self.supplier, purchase_date=day or self.today, subtotal=Decimal(total),
            taxable_amount=Decimal(total), grand_total=Decimal(total), amount_paid=Decimal(paid),
            status=status, created_by=self.admin,
        )
        return pu


class FrameworkTests(ReportBase):
    def test_every_ready_report_loads_on_an_empty_database(self):
        Sale.objects.all().delete()
        _, reports = get_sections()
        ready = [slug for slug, r in reports.items() if r["ready"]]
        self.assertGreaterEqual(len(ready), 22)
        for slug in ready:
            html, csv_ = self.report(slug), self.report(slug, format="csv")
            self.assertEqual(html.status_code, 200, slug)
            self.assertEqual(csv_.status_code, 200, slug)

    def test_every_ready_report_loads_with_data(self):
        sale = self.sell(customer=self.customer, discount="10")
        self.refund(sale)
        self.purchase("1000.00", paid="400", no="AF-P-T-2")
        _, reports = get_sections()
        for slug, r in reports.items():
            if r["ready"]:
                self.assertEqual(self.report(slug).status_code, 200, slug)
                self.assertEqual(self.report(slug, format="csv").status_code, 200, slug)

    def test_a_cashier_cannot_open_reports(self):
        self.client.logout()
        self.client.login(username="rp_cash", password="pw12345!")
        self.assertEqual(self.client.get(reverse("reports:index")).status_code, 403)
        self.assertEqual(self.report("sales-daily-sales").status_code, 403)
        self.assertEqual(self.report("sales-daily-sales", format="csv").status_code, 403)

    def test_invalid_dates_fall_back_with_a_message(self):
        response = self.report("sales-daily-sales", date_from="garbage")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("not valid" in n for n in response.context["notes"]))

    def test_a_filter_that_does_not_apply_is_called_out(self):
        response = self.report("sales-daily-sales", product=str(self.product.pk))
        self.assertTrue(any("does not apply" in n for n in response.context["notes"]))

    def test_csv_neutralises_formulas(self):
        Product.objects.create(name="=SUM(1)", sale_price=Decimal("10.00"))
        self.assertIn("'=SUM(1)", self.report("inventory-product-list", format="csv").content.decode())

    def test_row_cap_banner_and_full_totals(self):
        self.sell(qty="1")
        self.sell(qty="1")
        self.sell(qty="1")
        with mock.patch("reports.helpers.PAGE_CAP", 2):
            response = self.report("sales-bill-wise-sales-with-gst")
        self.assertTrue(response.context["truncated"])
        self.assertEqual(len(response.context["rows"]), 2)
        self.assertEqual(response.context["total_rows"], 3)
        self.assertEqual(response.context["report"]._totals()["total"], Decimal("750.00"))


class SalesReportTests(ReportBase):
    def test_daily_sales_agree_with_the_dashboard(self):
        sale = self.sell(qty="3")
        self.sell(qty="1")
        self.refund(sale)
        records = self.records("sales-daily-sales")
        m = _metrics(self.today, self.today)
        self.assertEqual(sum(r["net"] for r in records), m["sales"])
        self.assertEqual(sum(r["refund"] for r in records), m["returns"])
        self.assertEqual(sum(r["bills"] for r in records), m["bills"])

    def test_product_profit_agrees_with_the_dashboard(self):
        sale = self.sell(qty="3")
        self.sell(qty="2", discount="10")
        self.refund(sale, qty="2")
        records = self.records("sales-sales-by-product")
        self.assertEqual(sum(r["profit"] for r in records).quantize(Q2), _metrics(self.today, self.today)["profit"])
        self.assertEqual(records[0]["ret_qty"], Decimal("2"))

    def test_payment_totals_match_the_bills(self):
        self.sell(qty="3")
        self.sell(qty="1")
        records = self.records("sales-sales-by-payment-type")
        received = [r for r in records if r["type"] == "Total received"][0]["amt"]
        self.assertEqual(received, sum(s.total for s in Sale.objects.all()))
        self.assertEqual(received, Decimal("1000.00"))

    def test_collected_credit_payments_lists_existing_sale_payments(self):
        sale = self.sell(customer=self.customer)
        rows = self.records("sales-collected-credit-payments")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["invoice"], sale.invoice_no)
        self.assertEqual(rows[0]["customer"], self.customer.name)
        self.assertEqual(rows[0]["amount"], sale.total)

    def test_refunds_show_by_refund_method(self):
        sale = self.sell()
        self.refund(sale)
        records = self.records("sales-sales-by-payment-type")
        refunded = [r for r in records if r["type"] == "Refunded"]
        self.assertEqual(refunded[0]["method"], "UPI")
        self.assertEqual(refunded[0]["amt"], Decimal("250.00"))

    def test_gst_summary_matches_the_bills_and_the_invoice(self):
        sale = self.sell(qty="3", customer=self.customer)
        self.sell(qty="1")
        summary = self.records("sales-gst-summary-by-rate")
        self.assertEqual(sum(r["gst"] for r in summary), sum(s.tax_total for s in Sale.objects.all()))

        bill = [r for r in self.records("sales-bill-wise-sales-with-gst") if r["invoice"] == sale.invoice_no][0]
        invoice = self.client.get(reverse("pos:invoice", args=[sale.pk]))
        self.assertEqual(bill["cgst"], invoice.context["cgst_total"])
        self.assertEqual(bill["sgst"], invoice.context["sgst_total"])
        self.assertEqual(bill["cgst"] + bill["sgst"], bill["gst"])
        self.assertEqual(bill["gstin"], "24ABCDE1234F1Z5")
        self.assertEqual(sum(r["cgst"] for r in summary), sum(r["cgst"] for r in self.records("sales-bill-wise-sales-with-gst")))

    def test_voided_bills_are_excluded_but_listed_as_voided(self):
        keep, gone = self.sell(), self.sell()
        self.client.post(reverse("pos:sale_void", args=[gone.pk]), {"reason": "wrong bill"})
        daily = self.records("sales-daily-sales")
        self.assertEqual(sum(r["bills"] for r in daily), 1)
        voided = self.records("sales-voided-items")
        self.assertEqual([v["invoice"] for v in voided], [gone.invoice_no])
        self.assertEqual(voided[0]["reason"], "wrong bill")
        self.assertNotIn(keep.invoice_no, [v["invoice"] for v in voided])

    def test_returns_report_lists_the_refund(self):
        sale = self.sell()
        self.refund(sale)
        rows = self.records("sales-sales-returns")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["refund"], Decimal("250.00"))
        self.assertEqual(rows[0]["invoice"], sale.invoice_no)

    def test_discount_reports_list_only_discounted_sales(self):
        plain = self.sell()
        cut = self.sell(discount="10")
        granted = self.records("sales-discounts-granted")
        self.assertEqual([r["invoice"] for r in granted], [cut.invoice_no])
        self.assertEqual(granted[0]["disc"], Decimal("50.00"))
        self.assertEqual(len(self.records("sales-items-discounts")), 1)
        self.assertNotIn(plain.invoice_no, [r["invoice"] for r in granted])

    def test_customer_filter_limits_the_bills(self):
        self.sell(customer=self.customer)
        self.sell()
        rows = self.records("sales-bill-wise-sales-with-gst", party=str(self.customer.pk))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["customer"], "Asha")


class PurchaseReportTests(ReportBase):
    def test_cancelled_purchases_are_excluded_and_listed_as_cancelled(self):
        self.purchase("1000.00", paid="400", no="AF-P-T-1")
        self.purchase("500.00", status="cancelled", no="AF-P-T-2")
        suppliers = self.records("purchase-suppliers")
        self.assertEqual(suppliers[0]["total"], Decimal("1000.00"))
        self.assertEqual(suppliers[0]["out"], Decimal("600.00"))
        listing = self.report("purchase-purchase-invoice-list")
        self.assertEqual(len(listing.context["report"].records), 2)
        self.assertEqual(listing.context["report"]._totals()["total"], Decimal("1000.00"))

    def test_unpaid_ignores_the_dates_and_counts_days(self):
        old = self.today - timedelta(days=45)
        self.purchase("1000.00", paid="200", day=old, no="AF-P-T-1")
        self.purchase("300.00", paid="300", no="AF-P-T-2")
        rows = self.records("purchase-unpaid-purchase", date_from=str(self.today), date_to=str(self.today))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["out"], Decimal("800.00"))
        self.assertEqual(rows[0]["days"], 45)

    def test_payment_types_include_money_paid_on_cancelled_purchases(self):
        live = self.purchase("1000.00", no="AF-P-T-1")
        dead = self.purchase("500.00", status="cancelled", no="AF-P-T-2")
        for pu, amount in ((live, "300.00"), (dead, "100.00")):
            PurchasePayment.objects.create(purchase=pu, method="cash", amount=Decimal(amount),
                                           payment_date=self.today, recorded_by=self.admin)
        rows = self.records("purchase-payment-types")
        self.assertEqual(rows[0]["amt"], Decimal("400.00"))
        self.assertEqual(rows[0]["cx"], Decimal("100.00"))
        by_supplier = self.records("purchase-payment-types-by-supplier")
        self.assertEqual(by_supplier[0]["name"], "Resin Co")

    def test_item_reports_and_tax_rates(self):
        pu = self.purchase("1180.00", no="AF-P-T-1")
        PurchaseItem.objects.create(
            purchase=pu, product=self.product, product_name="Resin Clear", quantity=Decimal("10"),
            cost_price=Decimal("100.00"), discount=Decimal("100.00"), gst_rate=Decimal("18"),
            taxable_amount=Decimal("900.00"), gst_amount=Decimal("162.00"), total_amount=Decimal("1062.00"),
        )
        products = self.records("purchase-products")
        self.assertEqual(products[0]["qty"], Decimal("10"))
        self.assertEqual(products[0]["avg"], Decimal("90"))
        self.assertEqual(self.records("purchase-tax-rates")[0]["gst"], Decimal("162.00"))
        self.assertEqual(self.records("purchase-purchased-items-discounts")[0]["disc"], Decimal("100.00"))


class StockReportTests(ReportBase):
    def test_low_stock_and_reorder_use_the_stock_level(self):
        rows = self.records("stock-control-low-stock-warning")
        self.assertEqual(rows[0]["name"], "Resin Clear")          # 100 in stock, level 200
        reorder = self.records("stock-control-reorder-product-list")
        self.assertEqual(reorder[0]["short"], Decimal("100"))
        self.assertEqual(reorder[0]["est"], Decimal("10000.00"))

    def test_loss_and_damage_sums_damage_movements(self):
        StockMovement.objects.create(product=self.product, quantity=-3, movement_type="damage", note="broken")
        rows = self.records("loss-and-damage-products")
        self.assertEqual(rows[0]["qty"], Decimal("3"))
        self.assertEqual(rows[0]["value"], Decimal("300.00"))

    def test_stock_return_report_uses_purchase_return_movements(self):
        StockMovement.objects.create(
            product=self.product,
            quantity=-2,
            movement_type=StockMovement.Type.PURCHASE_RETURN,
            reference="AF-P-T-1",
            note="Purchase cancelled",
        )
        rows = self.records("stock-return-products")
        self.assertEqual(rows[0]["qty"], Decimal("2"))
        self.assertEqual(rows[0]["value"], Decimal("200.00"))

    def test_stock_movement_shows_a_running_balance_for_one_product(self):
        self.sell(qty="2")
        rows = self.records("sales-stock-movement", product=str(self.product.pk))
        self.assertEqual(rows[0]["type"], "Balance before this period")
        self.assertEqual(rows[-1]["balance"], Decimal("98"))


class FinanceReportTests(ReportBase):
    def test_transaction_history_includes_sale_supplier_payment_and_expense(self):
        self.sell(customer=self.customer)
        purchase = self.purchase("1000.00", no="AF-P-T-3")
        PurchasePayment.objects.create(
            purchase=purchase,
            method="bank_transfer",
            amount=Decimal("300.00"),
            payment_date=self.today,
            recorded_by=self.admin,
        )
        category = ExpenseCategory.objects.create(name="Rent")
        Expense.objects.create(
            expense_no="AF-E-T-1",
            expense_date=self.today,
            category=category,
            amount=Decimal("50.00"),
            payment_method=Expense.PaymentMethod.CASH,
            created_by=self.admin,
        )

        report = self.report("finance-transaction-history")
        rows = report.context["report"].records
        self.assertEqual({row["type"] for row in rows}, {"Sale payment", "Supplier payment", "Expense - Rent"})
        self.assertEqual(report.context["report"]._totals()["inflow"], Decimal("500.00"))
        self.assertEqual(report.context["report"]._totals()["outflow"], Decimal("350.00"))

    def test_profit_and_loss_matches_dashboard_profit_less_expenses(self):
        self.sell(qty="2")
        self.sell(qty="1")
        self.refund(Sale.objects.order_by("created_at", "id").first(), token="pnl-return")
        category = ExpenseCategory.objects.create(name="Rent")
        Expense.objects.create(
            expense_no="AF-E-T-2",
            expense_date=self.today,
            category=category,
            amount=Decimal("50.00"),
            payment_method=Expense.PaymentMethod.CASH,
            created_by=self.admin,
        )

        rows = {
            row["item"]: row["amount"]
            for row in self.records("finance-profit-and-loss")
        }
        self.assertEqual(
            rows["Net sales (excluding GST and returns)"]
            - rows["Cost of goods sold (net of returned-item cost)"],
            rows["Gross profit after returns"],
        )
        self.assertEqual(rows["Gross profit after returns"], _metrics(self.today, self.today)["profit"])
        self.assertEqual(rows["Operating expenses"], Decimal("50.00"))
        self.assertEqual(
            rows["Net profit"],
            _metrics(self.today, self.today)["profit"] - Decimal("50.00"),
        )


class CustomerReportTests(ReportBase):
    def test_customer_report_routes_and_navigation_selection(self):
        for route_name, slug in (
            ("reports:sales_by_customer", "customers-sales-by-customer"),
            ("reports:invoice_list", "customers-invoice-list"),
            ("reports:item_list", "customers-item-list"),
        ):
            self.assertEqual(self.client.get(reverse(route_name)).status_code, 200)
            self.assertEqual(
                self.client.get(reverse("reports:index"), {"selected": slug}).status_code,
                200,
            )
        index = self.client.get(
            reverse("reports:index"),
            {"selected": "customers-invoice-list"},
        )
        self.assertContains(index, 'class="rp-item active"')
        self.assertContains(index, "Invoice list")

    def test_sales_by_customer_aggregates_taxable_and_total_with_filter_aliases(self):
        sale = self.sell(customer=self.customer)
        rows = self.records(
            "customers-sales-by-customer",
            start_date=self.today.isoformat(),
            end_date=self.today.isoformat(),
            customer_id=str(self.customer.pk),
            user_id=str(self.admin.pk),
            product_id=str(self.product.pk),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["customer"], self.customer.name)
        self.assertEqual(rows[0]["before_tax"], sale.total - sale.tax_total)
        self.assertEqual(rows[0]["total"], sale.total)

    def test_invoice_list_filters_payment_method_and_has_sequential_numbers(self):
        sale = self.sell(customer=self.customer)
        rows = self.records(
            "customers-invoice-list",
            payment_method=Payment.Method.CASH,
            customer_id=str(self.customer.pk),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["number"], 1)
        self.assertEqual(rows[0]["document"], sale.invoice_no)
        self.assertEqual(rows[0]["method"], "Cash")
        self.assertEqual(rows[0]["tax"], sale.tax_total)
        self.assertEqual(rows[0]["total"], sale.total)
        self.assertEqual(self.records("customers-invoice-list", payment_method=Payment.Method.UPI), [])

    def test_item_list_totals_quantity_and_amount_and_labels_walk_in(self):
        self.sell(qty="2")
        rows = self.records("customers-item-list", product_id=str(self.product.pk))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["customer"], "Walk-in customer")
        self.assertEqual(rows[0]["quantity"], Decimal("2"))
        self.assertEqual(rows[0]["unit_price"], self.product.sale_price)
        self.assertEqual(rows[0]["total"], Decimal("500.00"))
        report = self.report("customers-item-list")
        self.assertEqual(report.context["report"]._totals()["quantity"], Decimal("2"))
        self.assertEqual(report.context["report"]._totals()["total"], Decimal("500.00"))
        self.assertContains(report, "CUSTOMER ITEM LIST")
        self.assertContains(report, "Company:")
        self.assertContains(report, "Address:")