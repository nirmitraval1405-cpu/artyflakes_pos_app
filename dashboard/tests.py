from datetime import timedelta

from django.utils import timezone

from people.models import Contact
from purchase.models import Purchase


import json
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from expenses.models import ExpenseCategory
from expenses.services import create_expense, void_expense
from inventory.models import Product, StockMovement

from .views import _change, inr, period_bounds


class HelperTests(TestCase):
    def test_indian_number_grouping(self):
        self.assertEqual(inr(Decimal("0")), "\u20b90.00")
        self.assertEqual(inr(Decimal("999.5")), "\u20b9999.50")
        self.assertEqual(inr(Decimal("12345")), "\u20b912,345.00")
        self.assertEqual(inr(Decimal("1234567.5")), "\u20b912,34,567.50")
        self.assertEqual(inr(Decimal("-250")), "-\u20b9250.00")

    def test_percent_change(self):
        self.assertEqual(_change(150, 100, "x")["text"], "+50.0% vs x")
        self.assertEqual(_change(50, 100, "x")["dir"], "down")
        self.assertEqual(_change(100, 100, "x")["dir"], "flat")
        self.assertEqual(_change(0, 0, "x")["dir"], "flat")
        self.assertEqual(_change(10, 0, "x")["dir"], "up")

    def test_today_compares_with_yesterday(self):
        cur, prev = period_bounds("today", date(2026, 11, 6))
        self.assertEqual(cur, (date(2026, 11, 6), date(2026, 11, 6)))
        self.assertEqual(prev, (date(2026, 11, 5), date(2026, 11, 5)))

    def test_week_is_seven_days_and_previous_is_the_seven_before(self):
        cur, prev = period_bounds("week", date(2026, 11, 10))
        self.assertEqual(cur, (date(2026, 11, 4), date(2026, 11, 10)))
        self.assertEqual(prev, (date(2026, 10, 28), date(2026, 11, 3)))

    def test_month_compares_the_same_number_of_days(self):
        cur, prev = period_bounds("month", date(2026, 11, 6))
        self.assertEqual(cur, (date(2026, 11, 1), date(2026, 11, 6)))
        self.assertEqual(prev, (date(2026, 10, 1), date(2026, 10, 6)))

    def test_month_end_does_not_run_past_a_shorter_previous_month(self):
        cur, prev = period_bounds("month", date(2026, 3, 31))
        self.assertEqual(prev, (date(2026, 2, 1), date(2026, 2, 28)))

    def test_year_handles_29_february(self):
        cur, prev = period_bounds("year", date(2028, 2, 29))
        self.assertEqual(prev, (date(2027, 1, 1), date(2027, 2, 28)))


class DashboardTests(TestCase):
    def setUp(self):
        User.objects.create_user("cash9", password="pw12345!", role="cashier")
        User.objects.create_user("boss9", password="pw12345!", role="admin")
        self.product = Product.objects.create(
            name="Resin", sale_price=Decimal("250.00"), mrp=Decimal("300.00"),
            cost_price=Decimal("100.00"), gst_rate=Decimal("18"),
        )
        StockMovement.objects.create(product=self.product, quantity=10, movement_type="opening")

    def sell(self):
        body = {
            "items": [{"product_id": self.product.pk, "quantity": "2"}],
            "payments": [{"method": "cash", "amount": "500.00"}],
        }
        response = self.client.post(reverse("pos:checkout"), data=json.dumps(body), content_type="application/json")
        from pos.models import Sale
        return Sale.objects.get(invoice_no=response.json()["invoice_no"])

    def cards(self):
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.status_code, 200)
        cards = {c["label"]: c for c in response.context["primary_cards"] + response.context["secondary_cards"]}
        return cards, response

    def test_cashier_is_sent_to_the_pos_screen(self):
        self.client.login(username="cash9", password="pw12345!")
        response = self.client.get(reverse("dashboard:home"))
        self.assertRedirects(response, reverse("pos:screen"))

    def test_empty_dashboard_has_no_errors(self):
        self.client.login(username="boss9", password="pw12345!")
        cards, response = self.cards()
        self.assertEqual(cards["Total Sales"]["display"], "\u20b90.00")
        self.assertEqual(cards["Bills Made"]["display"], "0")
        self.assertEqual(cards["Total Purchase"]["display"], "\u20b90.00")
        self.assertEqual(cards["Purchase Return"]["display"], "\u2014")
        self.assertEqual(cards["Net Profit"]["display"], "\u20b90.00")
        self.assertEqual(cards["Total Expenses"]["display"], "\u20b90.00")
        self.assertNotContains(response, "Phase 7")
        self.assertNotContains(response, "Phase 8")

    def test_sales_and_profit_are_calculated_without_gst(self):
        self.client.login(username="boss9", password="pw12345!")
        self.sell()                                         # 2 x 250 = 500, GST 76.27, cost 200
        cards, response = self.cards()
        self.assertEqual(cards["Total Sales"]["display"], "\u20b9500.00")
        self.assertEqual(cards["Bills Made"]["display"], "1")
        self.assertEqual(cards["Gross Profit"]["display"], "\u20b9223.73")   # 500 - 76.27 - 200
        self.assertEqual(response.context["top_products"][0]["name"], "Resin")
        self.assertEqual(response.context["heat"]["total"], 1)

    def test_expenses_reduce_net_profit_and_appear_in_hourly_chart(self):
        self.client.force_login(User.objects.get(username="boss9"))
        self.sell()
        category = ExpenseCategory.objects.create(name="Rent")
        admin_user = User.objects.get(username="boss9")
        create_expense(admin_user, {
            "expense_date": timezone.localdate(),
            "category": category.pk,
            "amount": "300.00",
            "payment_method": "cash",
            "client_token": "dashboard-rent",
        })
        cards, response = self.cards()
        self.assertEqual(cards["Total Expenses"]["display"], "\u20b9300.00")
        self.assertEqual(cards["Net Profit"]["display"], "-\u20b976.27")
        chart = response.context["chart_data"]["sales_statistics"]
        today_index = timezone.localtime(timezone.now()).hour
        self.assertEqual(len(chart["labels"]), 24)
        self.assertEqual(chart["revenue"][today_index], "500.00")
        self.assertEqual(chart["expenses"][today_index], "300.00")

    def test_sales_statistics_api_switches_aggregation_for_each_period(self):
        self.client.force_login(User.objects.get(username="boss9"))
        url = reverse("dashboard:sales_statistics")
        today = timezone.localdate()
        expected_lengths = {
            "today": 24,
            "week": 7,
            "days30": 30,
            "month": today.day,
            "year": today.month,
        }
        for key, expected_length in expected_lengths.items():
            with self.subTest(key=key):
                response = self.client.get(url, {"range": key})
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(len(data["labels"]), expected_length)
                self.assertEqual(len(data["revenue"]), expected_length)
                self.assertEqual(len(data["expenses"]), expected_length)
                self.assertEqual(len(data["tooltips"]), expected_length)
        today_data = self.client.get(url, {"range": "today"}).json()
        self.assertEqual(today_data["labels"][0], "12 AM")
        self.assertEqual(today_data["labels"][-1], "11 PM")
        year_data = self.client.get(url, {"range": "year"}).json()
        self.assertEqual(year_data["labels"][0], "Jan")
        self.assertEqual(year_data["labels"][-1], today.strftime("%b"))

    def test_sales_statistics_api_rejects_cashiers_and_invalid_ranges(self):
        url = reverse("dashboard:sales_statistics")
        self.client.force_login(User.objects.get(username="cash9"))
        self.assertEqual(self.client.get(url, {"range": "today"}).status_code, 403)
        self.client.force_login(User.objects.get(username="boss9"))
        self.assertEqual(self.client.get(url, {"range": "invalid"}).status_code, 400)

    def test_voided_expense_is_not_counted_on_dashboard(self):
        self.client.force_login(User.objects.get(username="boss9"))
        category = ExpenseCategory.objects.create(name="Utilities")
        admin_user = User.objects.get(username="boss9")
        expense, _ = create_expense(admin_user, {
            "expense_date": timezone.localdate(),
            "category": category.pk,
            "amount": "100.00",
            "payment_method": "cash",
            "client_token": "dashboard-voided",
        })
        void_expense(admin_user, expense.pk, "Entered in error")
        cards, _ = self.cards()
        self.assertEqual(cards["Total Expenses"]["display"], "\u20b90.00")
        self.assertEqual(cards["Net Profit"]["display"], "\u20b90.00")

    def test_a_return_reduces_profit_and_shows_as_sales_return(self):
        self.client.login(username="boss9", password="pw12345!")
        sale = self.sell()
        item = sale.items.get()
        self.client.post(
            reverse("pos:sale_return", args=[sale.pk]),
            {"refund_method": "cash", "client_token": "d1", f"qty_{item.pk}": "1"},
        )
        cards, _ = self.cards()
        self.assertEqual(cards["Sales Return"]["display"], "\u20b9250.00")
        self.assertEqual(cards["Gross Profit"]["display"], "\u20b9111.87")   # 223.73 - (250 - 38.14 - 100)

    def test_voided_bills_are_not_counted(self):
        self.client.login(username="boss9", password="pw12345!")
        sale = self.sell()
        self.client.post(reverse("pos:sale_void", args=[sale.pk]), {"reason": "test"})
        cards, response = self.cards()
        self.assertEqual(cards["Total Sales"]["display"], "\u20b90.00")
        self.assertEqual(cards["Bills Made"]["display"], "0")
        self.assertEqual(response.context["overall"]["orders"], 0)

    def test_every_period_loads(self):
        self.client.login(username="boss9", password="pw12345!")
        self.sell()
        for key in ("today", "week", "days30", "month", "year", "nonsense"):
            self.assertEqual(self.client.get(reverse("dashboard:home"), {"range": key}).status_code, 200)

    def test_named_customer_is_first_time_then_returning(self):
        from people.models import Contact
        self.client.login(username="boss9", password="pw12345!")
        asha = Contact.objects.create(name="Asha", phone="9876543210")
        body = {
            "customer_id": asha.pk,
            "items": [{"product_id": self.product.pk, "quantity": "1"}],
            "payments": [{"method": "cash", "amount": "250.00"}],
        }
        self.client.post(reverse("pos:checkout"), data=json.dumps(body), content_type="application/json")
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.context["overall"]["first_time"], 1)
        self.assertEqual(response.context["top_customers"][0]["name"], "Asha")
        
class DashboardPurchaseTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("dp_boss", password="pw12345!", role="admin")
        self.supplier = Contact.objects.create(name="Resin Co", is_customer=False, is_supplier=True)
        self.client.login(username="dp_boss", password="pw12345!")
        self.today = timezone.localdate()
        self.count = 0

    def buy(self, amount, day=None, status="completed"):
        self.count += 1
        return Purchase.objects.create(
            purchase_no=f"AF-P-T-{self.count}", supplier=self.supplier, purchase_date=day or self.today,
            grand_total=Decimal(amount), status=status, created_by=self.admin,
        )

    def get(self, key="today"):
        response = self.client.get(reverse("dashboard:home"), {"range": key})
        cards = {c["label"]: c for c in response.context["primary_cards"]}
        return cards["Total Purchase"], response

    def test_only_completed_purchases_are_counted(self):
        self.buy("1000.00")
        self.buy("400.00", status="cancelled")
        card, _ = self.get()
        self.assertEqual(card["display"], "\u20b91,000.00")

    def test_period_and_percent_change(self):
        self.buy("150.00")
        self.buy("100.00", day=self.today - timedelta(days=1))
        self.buy("999.00", day=self.today - timedelta(days=40))        # outside the week
        card, _ = self.get("today")
        self.assertEqual(card["change"]["text"], "+50.0% vs yesterday")
        card, _ = self.get("week")
        self.assertEqual(card["display"], "\u20b9250.00")

    def test_chart_has_a_purchase_series_except_for_today(self):
        self.buy("150.00")
        _, response = self.get("today")
        self.assertIsNone(response.context["chart_data"]["purchases"])
        self.assertEqual(response.context["today_purchases"], "\u20b9150.00")

        _, response = self.get("week")
        series = response.context["chart_data"]["purchases"]
        self.assertEqual(len(series), 7)
        self.assertEqual(series[-1], 150.0)

        _, response = self.get("year")
        self.assertEqual(response.context["chart_data"]["purchases"][self.today.month - 1], 150.0)

    def test_an_empty_database_shows_zero_not_an_error(self):
        card, response = self.get("month")
        self.assertEqual(card["display"], "\u20b90.00")
        self.assertEqual(response.status_code, 200)