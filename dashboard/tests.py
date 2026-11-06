import json
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
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
        self.assertEqual(cards["Total Purchase"]["display"], "\u2014")
        self.assertEqual(cards["Purchase Return"]["display"], "\u2014")
        self.assertEqual(cards["Total Sales Due"]["display"], "\u2014")
        self.assertEqual(cards["Total Expenses"]["display"], "\u2014")
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