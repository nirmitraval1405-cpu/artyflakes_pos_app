from datetime import date
from types import SimpleNamespace

from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import User


class DashboardTemplateTests(SimpleTestCase):
    def render_dashboard(self, low_stock):
        user = SimpleNamespace(
            is_authenticated=True,
            is_admin_role=False,
            username="test-user",
            get_full_name=lambda: "",
            get_role_display=lambda: "Cashier",
        )
        request = SimpleNamespace(
            resolver_match=SimpleNamespace(app_name="dashboard", view_name="dashboard:home")
        )
        response_html = get_template("dashboard/home.html").render({
            "request": request,
            "user": user,
            "today": date(2026, 11, 5),
            "primary_cards": [],
            "secondary_cards": [],
            "overall": {"suppliers": 0, "customers": 0},
            "low_stock": low_stock,
            "messages": [],
        })
        return response_html

    def test_dashboard_renders_included_empty_states(self):
        response_html = self.render_dashboard([])

        self.assertNotIn("{%", response_html)
        self.assertNotIn("{{", response_html)
        for section in (
            "Sales & Purchase",
            "Top Selling Products",
            "Recent Sales",
            "Sales Statistics",
            "Recent Transactions",
            "Top Customers",
            "Top Categories",
            "Order Statistics",
        ):
            with self.subTest(section=section):
                self.assertIn(section, response_html)

    def test_dashboard_renders_populated_low_stock_state(self):
        response_html = self.render_dashboard([
            {"name": "Test product", "code": "ART-00001", "stock": "2", "out": False},
        ])

        self.assertIn("Low Stock Products", response_html)
        self.assertIn("Test product", response_html)
        self.assertNotIn("Nothing is running low", response_html)


class DashboardViewTests(TestCase):
    def test_dashboard_view_renders_template_instead_of_raw_source(self):
        user = User.objects.create_user(username="dashboard-test", password="test-password")
        self.client.force_login(user)

        response = self.client.get(reverse("dashboard:home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sales & Purchase")
        self.assertNotContains(response, "{% include")
