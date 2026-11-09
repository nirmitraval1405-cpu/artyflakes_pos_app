import json
from decimal import Decimal

from django.db import transaction
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from inventory.models import Product, StockMovement
from inventory.stock import current_stock
from people.models import Contact

from .models import InvoiceCounter, Payment, Sale
from .services import compute_bill, div_round


class InvoiceCounterTests(TestCase):
    def test_numbers_increase_without_gaps(self):
        with transaction.atomic():
            first = InvoiceCounter.next_invoice_no()
            second = InvoiceCounter.next_invoice_no()
        self.assertTrue(first.endswith("-0001"))
        self.assertTrue(second.endswith("-0002"))


class BillMathTests(TestCase):
    def line(self, price, qty=1000, gst=0, kind=None, value=0):
        return {"price": price, "qty_milli": qty, "gst_bp": gst, "disc_kind": kind, "disc_value": value}

    def test_rounding_is_half_up(self):
        self.assertEqual(div_round(5, 2), 3)
        self.assertEqual(div_round(4, 2), 2)

    def test_gst_is_contained_in_the_price(self):
        calc = compute_bill([self.line(25000, gst=1800)])
        self.assertEqual(calc["total"], 25000)       # Rs 250 stays Rs 250
        self.assertEqual(calc["tax_total"], 3814)    # of which Rs 38.14 is GST

    def test_percent_discount_reduces_total_and_tax(self):
        calc = compute_bill([self.line(25000, gst=1800, kind="percent", value=1000)])
        self.assertEqual(calc["discount_total"], 2500)
        self.assertEqual(calc["total"], 22500)
        self.assertEqual(calc["tax_total"], 3432)

    def test_bill_discount_is_shared_and_adds_up(self):
        calc = compute_bill([self.line(10000), self.line(20000)], "amount", 1000)
        self.assertEqual(calc["bill_discount"], 1000)
        self.assertEqual(sum(l["discount"] for l in calc["lines"]), 1000)
        self.assertEqual([l["total"] for l in calc["lines"]], [9666, 19334])
        self.assertEqual(calc["total"], 29000)

    def test_discount_cannot_exceed_the_line(self):
        calc = compute_bill([self.line(1000, kind="amount", value=5000)])
        self.assertEqual(calc["discount_total"], 1000)
        self.assertEqual(calc["total"], 0)


class ProductSearchTests(TestCase):
    def setUp(self):
        User.objects.create_user("cashier1", password="pw12345!", role="cashier")
        self.client.login(username="cashier1", password="pw12345!")
        self.resin = Product.objects.create(name="Resin Clear", barcode="8901234", sale_price=250, mrp=300)
        Product.objects.create(name="Resin Old", is_active=False, sale_price=100)
        StockMovement.objects.create(product=self.resin, quantity=5, movement_type="opening")

    def search(self, q):
        return self.client.get(reverse("pos:product_search"), {"q": q}).json()["results"]

    def test_barcode_is_exact_match_with_price_in_paise(self):
        results = self.search("8901234")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["exact"])
        self.assertEqual(results[0]["price"], 25000)
        self.assertEqual(results[0]["stock"], 5.0)

    def test_inactive_products_are_hidden(self):
        names = [p["name"] for p in self.search("Resin")]
        self.assertIn("Resin Clear", names)
        self.assertNotIn("Resin Old", names)

    def test_search_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("pos:product_search"), {"q": "resin"})
        self.assertEqual(response.status_code, 302)


class CheckoutTests(TestCase):
    def setUp(self):
        self.cashier = User.objects.create_user("cash1", password="pw12345!", role="cashier")
        self.admin = User.objects.create_user("boss", password="pw12345!", role="admin")
        self.client.login(username="cash1", password="pw12345!")
        self.product = Product.objects.create(
            name="Resin", sale_price=Decimal("250.00"), mrp=Decimal("300.00"), gst_rate=Decimal("18"),
        )
        StockMovement.objects.create(product=self.product, quantity=5, movement_type="opening")
        self.url = reverse("pos:checkout")

    def body(self, **over):
        data = {
            "items": [{"product_id": self.product.pk, "quantity": "2"}],
            "payments": [{"method": "cash", "amount": "500.00"}],
        }
        data.update(over)
        return data

    def post(self, data):
        return self.client.post(self.url, data=json.dumps(data), content_type="application/json")

    def test_sale_is_saved_and_stock_goes_down(self):
        response = self.post(self.body())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["invoice_no"].endswith("-0001"))
        self.assertEqual(data["total"], "500.00")

        sale = Sale.objects.get()
        self.assertEqual(sale.total, Decimal("500.00"))
        self.assertEqual(sale.tax_total, Decimal("76.27"))
        self.assertTrue(sale.customer.is_system)       # Walk-in customer by default
        self.assertEqual(current_stock(self.product), 3)

        move = StockMovement.objects.get(movement_type="sale")
        self.assertEqual(move.quantity, Decimal("-2"))
        self.assertEqual(move.reference, sale.invoice_no)

    def test_item_copies_price_and_gst_at_time_of_sale(self):
        self.post(self.body())
        item = Sale.objects.get().items.get()
        self.assertEqual(item.product_name, "Resin")
        self.assertEqual(item.unit_price, Decimal("250.00"))
        self.assertEqual(item.gst_rate, Decimal("18.00"))
        self.assertEqual(item.line_total, Decimal("500.00"))

    def test_cash_change_is_returned_and_payment_adds_up(self):
        data = self.post(self.body(payments=[{"method": "cash", "amount": "600.00"}])).json()
        self.assertEqual(data["change"], "100.00")
        sale = Sale.objects.get()
        self.assertEqual(sale.amount_paid, Decimal("600.00"))
        self.assertEqual(sale.payments.get().amount, Decimal("500.00"))

    def test_split_payment(self):
        pays = [{"method": "cash", "amount": "200.00"}, {"method": "upi", "amount": "300.00", "reference": "T123"}]
        self.assertEqual(self.post(self.body(payments=pays)).status_code, 200)
        total = sum(p.amount for p in Payment.objects.all())
        self.assertEqual(total, Decimal("500.00"))

    def test_short_payment_is_rejected_and_nothing_is_saved(self):
        response = self.post(self.body(payments=[{"method": "cash", "amount": "100.00"}]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(current_stock(self.product), 5)

    def test_upi_cannot_be_overpaid(self):
        response = self.post(self.body(payments=[{"method": "upi", "amount": "600.00"}]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Sale.objects.count(), 0)

    def test_cashier_can_set_a_custom_price_and_sale_saves_it(self):
        data = self.body(
            items=[{"product_id": self.product.pk, "quantity": "2", "price": "125.00"}],
            payments=[{"method": "cash", "amount": "250.00"}],
        )
        response = self.post(data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], "250.00")
        item = Sale.objects.get().items.get()
        self.assertEqual(item.unit_price, Decimal("125.00"))
        self.assertEqual(item.line_total, Decimal("250.00"))
        self.assertEqual(current_stock(self.product), 3)

    def test_cashier_can_set_a_custom_price_above_list_price(self):
        data = self.body(
            items=[{"product_id": self.product.pk, "quantity": "2", "price": "275.00"}],
            payments=[{"method": "cash", "amount": "550.00"}],
        )
        response = self.post(data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], "550.00")
        self.assertEqual(Sale.objects.get().items.get().unit_price, Decimal("275.00"))

    def test_wrong_expected_total_is_rejected(self):
        response = self.post(self.body(expected_total=12345))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Sale.objects.count(), 0)

    def test_same_token_cannot_create_two_bills(self):
        self.post(self.body(client_token="abc123"))
        second = self.post(self.body(client_token="abc123"))
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(current_stock(self.product), 3)

    def test_selling_at_zero_stock_is_allowed_with_a_warning(self):
        glue = Product.objects.create(name="Glue", sale_price=Decimal("100.00"))
        data = {
            "items": [{"product_id": glue.pk, "quantity": "1"}],
            "payments": [{"method": "cash", "amount": "100.00"}],
        }
        response = self.post(data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["warnings"])
        self.assertEqual(current_stock(glue), -1)

    def test_inactive_product_cannot_be_sold(self):
        self.product.is_active = False
        self.product.save()
        self.assertEqual(self.post(self.body()).status_code, 400)

    def test_pieces_must_be_whole_numbers(self):
        data = self.body(items=[{"product_id": self.product.pk, "quantity": "1.5"}])
        self.assertEqual(self.post(data).status_code, 400)

    def test_cashier_discount_over_limit_is_rejected(self):
        item = {"product_id": self.product.pk, "quantity": "2", "discount": {"type": "percent", "value": "50"}}
        self.assertEqual(self.post(self.body(items=[item])).status_code, 400)
        self.assertEqual(Sale.objects.count(), 0)

    def test_admin_can_give_larger_discounts(self):
        self.client.logout()
        self.client.login(username="boss", password="pw12345!")
        item = {"product_id": self.product.pk, "quantity": "2", "discount": {"type": "percent", "value": "50"}}
        response = self.post(self.body(items=[item], payments=[{"method": "cash", "amount": "250.00"}]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], "250.00")

    def test_named_customer_is_saved_on_the_bill(self):
        customer = Contact.objects.create(name="Asha", phone="9876543210")
        self.post(self.body(customer_id=customer.pk))
        self.assertEqual(Sale.objects.get().customer, customer)

    def test_checkout_requires_login(self):
        self.client.logout()
        self.assertEqual(self.post(self.body()).status_code, 302)

    def test_customer_search_hides_walk_in_and_suppliers(self):
        Contact.walk_in()
        Contact.objects.create(name="Asha", phone="9876543210")
        Contact.objects.create(name="Vendor Co", is_customer=False, is_supplier=True)
        Contact.objects.create(name="Mixed Co", is_customer=True, is_supplier=True)
        names = [c["name"] for c in self.client.get(reverse("pos:customer_search")).json()["results"]]
        self.assertEqual(names, ["Asha"])


class BillPrintTests(TestCase):
    def setUp(self):
        User.objects.create_user("c1", password="pw12345!", role="cashier")
        User.objects.create_user("c2", password="pw12345!", role="cashier")
        User.objects.create_user("boss2", password="pw12345!", role="admin")
        product = Product.objects.create(
            name="Resin", sale_price=Decimal("250.00"), mrp=Decimal("300.00"),
            gst_rate=Decimal("18"), hsn_code="3906",
        )
        StockMovement.objects.create(product=product, quantity=5, movement_type="opening")

        self.client.login(username="c1", password="pw12345!")
        body = {
            "items": [{"product_id": product.pk, "quantity": "2"}],
            "payments": [{"method": "cash", "amount": "500.00"}],
        }
        response = self.client.post(reverse("pos:checkout"), data=json.dumps(body), content_type="application/json")
        self.result = response.json()
        self.sale = Sale.objects.get()

    def test_checkout_result_includes_lines_for_the_after_payment_screen(self):
        self.assertEqual(self.result["lines"][0]["name"], "Resin")
        self.assertEqual(self.result["lines"][0]["qty"], "2")
        self.assertIn("created", self.result)

    def test_receipt_shows_invoice_number_and_total(self):
        response = self.client.get(reverse("pos:receipt", args=[self.sale.pk]))
        self.assertContains(response, self.sale.invoice_no)
        self.assertContains(response, "500.00")

    def test_invoice_shows_the_gst_split(self):
        response = self.client.get(reverse("pos:invoice", args=[self.sale.pk]))
        self.assertContains(response, "CGST")
        self.assertContains(response, "76.27")
        self.assertContains(response, "38.14")

    def test_another_cashier_cannot_open_the_bill(self):
        self.client.logout()
        self.client.login(username="c2", password="pw12345!")
        self.assertEqual(self.client.get(reverse("pos:receipt", args=[self.sale.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("pos:invoice", args=[self.sale.pk])).status_code, 404)

    def test_admin_can_open_any_bill(self):
        self.client.logout()
        self.client.login(username="boss2", password="pw12345!")
        self.assertEqual(self.client.get(reverse("pos:receipt", args=[self.sale.pk])).status_code, 200)

    def test_bills_require_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("pos:receipt", args=[self.sale.pk])).status_code, 302)

    def test_note_is_saved(self):
        url = reverse("pos:sale_note", args=[self.sale.pk])
        response = self.client.post(url, data=json.dumps({"note": "Gift wrap"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.note, "Gift wrap")