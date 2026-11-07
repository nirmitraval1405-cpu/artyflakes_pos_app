from datetime import date
from decimal import Decimal

from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from inventory.models import Product, StockMovement
from inventory.stock import current_stock
from people.models import Contact

from .models import Purchase
from .services import PurchaseError, add_payment, cancel_purchase, create_purchase, parse_lines


def post_of(rows):
    q = QueryDict(mutable=True)
    q.setlist("product", [str(r["product"]) for r in rows])
    for i, r in enumerate(rows):
        q[f"quantity_{i}"] = r.get("qty", "1")
        q[f"cost_price_{i}"] = r.get("cost", "100")
        q[f"discount_{i}"] = r.get("disc", "0")
        q[f"gst_rate_{i}"] = r.get("gst", "18")
    return q


class PurchaseServiceTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("p_boss", password="pw12345!", role="admin")
        self.supplier = Contact.objects.create(name="Resin Co", is_customer=False, is_supplier=True)
        self.resin = Product.objects.create(name="Resin", sale_price=Decimal("250.00"), unit="pcs")
        self.glue = Product.objects.create(name="Glue", sale_price=Decimal("100.00"), unit="kg")
        self.work = Product.objects.create(name="Custom work", sale_price=Decimal("500.00"), track_stock=False)
        self.header = {
            "supplier": self.supplier, "supplier_invoice_no": "INV-9",
            "purchase_date": date(2026, 11, 6), "notes": "",
        }

    def buy(self, rows, pay="0", method="cash", token=None):
        return create_purchase(self.admin, self.header, parse_lines(post_of(rows)), Decimal(pay), method, "", token)

    # ----- creating -----
    def test_totals_stock_and_cost_are_saved(self):
        purchase, _ = self.buy([{"product": self.resin.pk, "qty": "10", "cost": "100", "disc": "100", "gst": "18"}])
        self.assertEqual(purchase.subtotal, Decimal("1000.00"))
        self.assertEqual(purchase.discount_total, Decimal("100.00"))
        self.assertEqual(purchase.taxable_amount, Decimal("900.00"))
        self.assertEqual(purchase.grand_total, Decimal("1062.00"))
        self.assertEqual(purchase.outstanding_amount, Decimal("1062.00"))
        self.assertEqual(current_stock(self.resin), Decimal("10"))
        move = StockMovement.objects.get()
        self.assertEqual(move.movement_type, "purchase")
        self.assertEqual(move.reference, purchase.purchase_no)
        self.resin.refresh_from_db()
        self.assertEqual(self.resin.cost_price, Decimal("90.00"))      # 900 / 10, before GST

    def test_fractional_quantity_is_kept(self):
        self.buy([{"product": self.glue.pk, "qty": "0.125", "cost": "80", "gst": "0"}])
        self.assertEqual(current_stock(self.glue), Decimal("0.125"))

    def test_pieces_must_be_whole_numbers(self):
        with self.assertRaises(PurchaseError):
            self.buy([{"product": self.resin.pk, "qty": "1.5"}])
        self.assertEqual(Purchase.objects.count(), 0)

    def test_same_product_twice_is_rejected(self):
        with self.assertRaises(PurchaseError):
            self.buy([{"product": self.resin.pk}, {"product": self.resin.pk}])
        self.assertEqual(Purchase.objects.count(), 0)

    def test_bad_numbers_are_rejected(self):
        for bad in ({"cost": "-5"}, {"disc": "2000"}, {"gst": "150"}, {"qty": "0"}, {"cost": "abc"}):
            with self.assertRaises(PurchaseError):
                self.buy([{"product": self.resin.pk, **bad}])
        self.assertEqual(Purchase.objects.count(), 0)

    def test_untracked_product_makes_no_stock_row(self):
        self.buy([{"product": self.work.pk, "qty": "1", "cost": "500"}])
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_paying_more_than_the_total_is_rejected(self):
        with self.assertRaises(PurchaseError):
            self.buy([{"product": self.resin.pk, "qty": "10", "cost": "100"}], pay="5000")
        self.assertEqual(Purchase.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_unknown_payment_method_is_rejected(self):
        with self.assertRaises(PurchaseError):
            self.buy([{"product": self.resin.pk}], pay="10", method="cheque")

    def test_same_token_creates_one_purchase(self):
        rows = [{"product": self.resin.pk, "qty": "10"}]
        first, dup1 = self.buy(rows, token="same")
        second, dup2 = self.buy(rows, token="same")
        self.assertFalse(dup1)
        self.assertTrue(dup2)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Purchase.objects.count(), 1)
        self.assertEqual(current_stock(self.resin), Decimal("10"))

    # ----- paying the supplier -----
    def test_partial_payment_then_the_rest(self):
        purchase, _ = self.buy([{"product": self.resin.pk, "qty": "10", "cost": "100", "disc": "100"}], pay="500")
        self.assertEqual(purchase.payment_status, "Partial")
        self.assertEqual(purchase.outstanding_amount, Decimal("562.00"))

        add_payment(self.admin, purchase.pk, {"amount": "562.00", "method": "upi"})
        purchase.refresh_from_db()
        self.assertEqual(purchase.payment_status, "Paid")

        with self.assertRaises(PurchaseError):
            add_payment(self.admin, purchase.pk, {"amount": "1.00", "method": "cash"})

    def test_bad_payment_inputs_are_rejected(self):
        purchase, _ = self.buy([{"product": self.resin.pk, "qty": "10", "cost": "100"}])
        for bad in ({"amount": "0"}, {"amount": "-5"}, {"amount": "10", "method": "cheque"}, {"amount": "10", "payment_date": "not-a-date"}):
            with self.assertRaises(PurchaseError):
                add_payment(self.admin, purchase.pk, bad)

    # ----- cancelling -----
    def test_cancel_reverses_stock_once(self):
        purchase, _ = self.buy([{"product": self.resin.pk, "qty": "10"}])
        cancelled, warnings = cancel_purchase(self.admin, purchase.pk, "Wrong supplier")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(current_stock(self.resin), Decimal("0"))
        self.assertTrue(StockMovement.objects.filter(movement_type="purchase_return").exists())
        self.assertEqual(warnings, [])

        with self.assertRaises(PurchaseError):
            cancel_purchase(self.admin, purchase.pk, "again")
        self.assertEqual(current_stock(self.resin), Decimal("0"))

    def test_cannot_pay_a_cancelled_purchase(self):
        purchase, _ = self.buy([{"product": self.resin.pk, "qty": "10", "cost": "100"}])
        cancel_purchase(self.admin, purchase.pk, "x")
        with self.assertRaises(PurchaseError):
            add_payment(self.admin, purchase.pk, {"amount": "10", "method": "cash"})

    def test_cancel_warns_when_stock_goes_negative_or_money_was_paid(self):
        purchase, _ = self.buy([{"product": self.resin.pk, "qty": "10", "cost": "100"}], pay="200")
        StockMovement.objects.create(product=self.resin, quantity=-8, movement_type="sale", reference="AF-X")
        _, warnings = cancel_purchase(self.admin, purchase.pk, "x")
        text = " ".join(warnings)
        self.assertIn("Resin", text)
        self.assertIn("already paid", text)
        
class PurchaseFormTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("p_boss2", password="pw12345!", role="admin")
        User.objects.create_user("p_cash2", password="pw12345!", role="cashier")
        self.supplier = Contact.objects.create(name="Resin Co", is_customer=False, is_supplier=True)
        self.resin = Product.objects.create(name="Resin", sale_price=Decimal("250.00"))
        self.client.login(username="p_boss2", password="pw12345!")
        self.url = reverse("purchase:create")

    def data(self, **over):
        d = {
            "supplier": self.supplier.pk, "purchase_date": "2026-11-06", "payment_method": "cash",
            "client_token": "tok1", "product": [str(self.resin.pk)],
            "quantity_0": "2", "cost_price_0": "100", "discount_0": "0", "gst_rate_0": "18",
        }
        d.update(over)
        return d

    def test_saving_through_the_form_works(self):
        self.client.post(self.url, self.data())
        self.assertEqual(Purchase.objects.count(), 1)
        self.assertEqual(current_stock(self.resin), Decimal("2"))

    def test_a_failed_save_gives_the_typed_rows_back(self):
        response = self.client.post(self.url, self.data(cost_price_0="-5"))
        self.assertEqual(Purchase.objects.count(), 0)
        self.assertEqual(response.context["rows"][0]["product"], str(self.resin.pk))
        self.assertEqual(response.context["rows"][0]["qty"], "2")
        self.assertContains(response, "selected")

    def test_a_double_submit_with_the_same_token_saves_once(self):
        self.client.post(self.url, self.data())
        self.client.post(self.url, self.data())
        self.assertEqual(Purchase.objects.count(), 1)
        self.assertEqual(current_stock(self.resin), Decimal("2"))

    def test_the_form_contains_the_duplicate_guard_token(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'name="client_token"')

    def test_a_cashier_cannot_open_the_form(self):
        self.client.logout()
        self.client.login(username="p_cash2", password="pw12345!")
        self.assertEqual(self.client.get(self.url).status_code, 403)