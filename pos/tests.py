import json
from decimal import Decimal

from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from inventory.models import Category, Product, StockMovement

from .models import HeldSale, Sale, SaleItem


class POSAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="cashier", password="test-password")
        self.client.force_login(self.user)
        category = Category.objects.create(name="Test")
        self.product = Product.objects.create(
            name="Watercolor set",
            code="ART-TEST-1",
            barcode="123456789",
            category=category,
            sale_price=Decimal("10.00"),
            mrp=Decimal("12.00"),
            gst_rate=Decimal("5.00"),
        )
        StockMovement.objects.create(
            product=self.product,
            quantity=Decimal("3.000"),
            movement_type=StockMovement.Type.OPENING,
        )

    def post_json(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def test_search_finds_barcode_sku_and_name(self):
        for query in ("123456789", "ART-TEST", "watercolor"):
            response = self.client.get(reverse("pos:product_search"), {"q": query})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["results"][0]["id"], self.product.pk)

    def test_checkout_saves_sale_and_deducts_stock(self):
        response = self.post_json(reverse("pos:checkout"), {
            "items": [{"id": self.product.pk, "qty": "2", "discount": "1.00"}],
            "payment_method": "cash",
        })

        self.assertEqual(response.status_code, 201)
        sale = Sale.objects.get()
        item = SaleItem.objects.get(sale=sale)
        self.assertEqual(sale.subtotal, Decimal("20.00"))
        self.assertEqual(sale.discount, Decimal("2.00"))
        self.assertEqual(sale.tax, Decimal("0.90"))
        self.assertEqual(sale.total, Decimal("18.90"))
        self.assertEqual(item.total_amount, Decimal("18.90"))
        self.assertEqual(
            self.product.movements.aggregate(total=Sum("quantity"))["total"],
            Decimal("1.000"),
        )
        self.assertEqual(response.json()["sale"]["order_number"], sale.order_number)

    def test_checkout_rejects_insufficient_stock_without_writing_sale(self):
        response = self.post_json(reverse("pos:checkout"), {
            "items": [{"id": self.product.pk, "qty": "4", "discount": "0.00"}],
            "payment_method": "cash",
        })

        self.assertEqual(response.status_code, 409)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(SaleItem.objects.count(), 0)
        self.assertEqual(self.product.movements.count(), 1)

    def test_checkout_records_split_payment(self):
        response = self.post_json(reverse("pos:checkout"), {
            "items": [{"id": self.product.pk, "qty": "1", "discount": "0.00"}],
            "payment_method": "split",
            "payment_details": {"cash": "10.00", "card": "0.50", "upi": "0.00"},
        })

        self.assertEqual(response.status_code, 201)
        sale = Sale.objects.get()
        self.assertEqual(sale.total, Decimal("10.50"))
        self.assertEqual(sale.payment_details["card"], "0.50")

    def test_held_sale_can_be_restored_by_its_cashier(self):
        held_response = self.post_json(reverse("pos:hold_sale"), {
            "items": [{"id": self.product.pk, "name": self.product.name}],
            "customer_name": "A customer",
        })
        self.assertEqual(held_response.status_code, 201)
        held_id = held_response.json()["id"]
        self.assertEqual(HeldSale.objects.count(), 1)

        restore_response = self.post_json(
            reverse("pos:restore_held_sale", args=[held_id]), {}
        )
        self.assertEqual(restore_response.status_code, 200)
        self.assertEqual(restore_response.json()["items"][0]["id"], self.product.pk)
        self.assertEqual(HeldSale.objects.count(), 0)
