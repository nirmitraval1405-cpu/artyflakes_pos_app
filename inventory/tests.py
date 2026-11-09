from decimal import Decimal
import tempfile
from pathlib import Path

from django.core.files.storage import default_storage
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import User

from .forms import ProductForm
from .models import Brand, Category, Product, StockMovement, Unit
from .utils.barcode_generator import (
    create_barcode_image,
    generate_barcode,
    validate_barcode,
)


class BarcodeGeneratorTests(TestCase):
    def test_generates_numeric_code128_value_by_default(self):
        barcode = generate_barcode()

        self.assertRegex(barcode, r"^\d{12}$")
        self.assertEqual(validate_barcode(barcode), barcode)

    def test_uses_available_sku_and_falls_back_when_sku_is_taken(self):
        Product.objects.create(name="Existing", barcode="ART-123")

        self.assertEqual(generate_barcode(sku="ART-456"), "ART-456")
        self.assertRegex(generate_barcode(sku="ART-123"), r"^\d{12}$")
        self.assertRegex(generate_barcode(sku="SKU-आर्ट"), r"^\d{12}$")

    def test_rejects_duplicate_manual_values(self):
        Product.objects.create(name="Existing", barcode="123456789012")
        form = ProductForm(data={
            "name": "Duplicate",
            "barcode": "123456789012",
            "code": "",
            "unit": Product.Unit.PCS,
            "cost_price": "0",
            "sale_price": "0",
            "mrp": "0",
            "gst_rate": "0",
            "hsn_code": "",
            "min_stock": "0",
            "is_active": "on",
        })

        self.assertFalse(form.is_valid())
        self.assertIn("barcode", form.errors)

    def test_creates_png_image_in_media_storage(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            barcode = generate_barcode()
            image_name = create_barcode_image(barcode)

            self.assertTrue(default_storage.exists(image_name))
            with default_storage.open(image_name, "rb") as image_file:
                self.assertTrue(image_file.read(8).startswith(b"\x89PNG\r\n\x1a\n"))


class BarcodeEndpointTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.user = User.objects.create_superuser(username="admin", password="secret")
        self.client.force_login(self.user)

    def test_generation_endpoint_returns_value_and_image_url_with_csrf(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            self.client.get(reverse("inventory:product_create"))
            csrf_token = self.client.cookies["csrftoken"].value
            response = self.client.post(
                reverse("inventory:barcode_generate"),
                {"code": "SKU-123"},
                HTTP_X_CSRFTOKEN=csrf_token,
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["barcode"], "SKU-123")
            self.assertTrue(response.json()["image_url"].startswith("/media/barcodes/"))
            image_name = response.json()["image_url"].removeprefix("/media/")
            self.assertTrue(Path(default_storage.path(image_name)).exists())

    def test_barcode_generation_route_is_registered(self):
        self.assertEqual(reverse("inventory:barcode_generate"), "/inventory/products/barcode/generate/")
        response = self.client.get(reverse("inventory:product_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-generate-url="/inventory/products/barcode/generate/"')
        self.assertContains(response, "requestBarcode")
        self.assertContains(response, "Opening stock")
        self.assertContains(response, 'name="opening_stock"')

    def test_product_creation_keeps_optional_barcode_blank(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            self.client.get(reverse("inventory:product_create"))
            response = self.client.post(
                reverse("inventory:product_create"),
                {
                    "name": "Created product",
                    "barcode": "",
                    "code": "",
                    "unit": Product.Unit.PCS,
                    "cost_price": "0",
                    "sale_price": "0",
                    "mrp": "0",
                    "gst_rate": "0",
                    "hsn_code": "",
                    "min_stock": "0",
                    "is_active": "on",
                },
                HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value,
            )
            product = Product.objects.get(name="Created product")
            self.assertEqual(response.status_code, 302)
            self.assertIsNone(product.barcode)

    def test_product_creation_records_optional_opening_stock(self):
        self.client.get(reverse("inventory:product_create"))
        response = self.client.post(
            reverse("inventory:product_create"),
            {
                "name": "Stocked product",
                "barcode": "",
                "code": "",
                "unit": Product.Unit.PCS,
                "track_stock": "on",
                "opening_stock": "12.5",
                "cost_price": "0",
                "sale_price": "0",
                "mrp": "0",
                "gst_rate": "0",
                "hsn_code": "",
                "min_stock": "0",
                "is_active": "on",
            },
            HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value,
        )

        product = Product.objects.get(name="Stocked product")
        movement = product.movements.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(movement.quantity, 12.5)
        self.assertEqual(movement.movement_type, "opening")
        self.assertEqual(movement.created_by, self.user)

    def test_opening_stock_requires_stock_tracking(self):
        no_stock_form = ProductForm(data={
            "name": "Untracked service",
            "track_stock": "",
            "unit": Product.Unit.PCS,
            "cost_price": "0",
            "sale_price": "0",
            "mrp": "0",
            "gst_rate": "0",
            "min_stock": "0",
        })
        self.assertTrue(no_stock_form.is_valid(), no_stock_form.errors)
        form = ProductForm(data={
            "name": "Untracked product",
            "track_stock": "",
            "opening_stock": "3",
        })

        self.assertFalse(form.is_valid())
        self.assertIn("opening_stock", form.errors)

    def test_product_edit_preserves_existing_barcode(self):
        product = Product.objects.create(name="Before edit", barcode="EDIT-123")

        self.client.get(reverse("inventory:product_update", args=[product.pk]))
        response = self.client.post(
            reverse("inventory:product_update", args=[product.pk]),
            {
                "name": "After edit",
                "category": "",
                "brand": "",
                "barcode": product.barcode,
                "code": product.code,
                "unit": Product.Unit.PCS,
                "cost_price": "0",
                "sale_price": "0",
                "mrp": "0",
                "gst_rate": "0",
                "hsn_code": "",
                "min_stock": "0",
                "is_active": "on",
            },
            HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value,
        )

        product.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(product.name, "After edit")
        self.assertEqual(product.barcode, "EDIT-123")


class ProductDeleteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="inventory-admin",
            password="test-password",
            role=User.Role.ADMIN,
        )
        self.client.force_login(self.user)
        self.product = Product.objects.create(name="Historical product")
        self.delete_url = reverse("inventory:product_delete", args=[self.product.pk])

    def test_product_with_stock_history_is_archived(self):
        movement = StockMovement.objects.create(
            product=self.product,
            quantity=2,
            movement_type=StockMovement.Type.OPENING,
            created_by=self.user,
        )

        response = self.client.post(self.delete_url)

        self.assertRedirects(response, reverse("inventory:product_list"))
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)
        self.assertTrue(Product.objects.filter(pk=self.product.pk).exists())
        self.assertTrue(StockMovement.objects.filter(pk=movement.pk).exists())

    def test_confirmation_explains_historical_product_will_be_archived(self):
        StockMovement.objects.create(
            product=self.product,
            quantity=2,
            movement_type=StockMovement.Type.OPENING,
        )

        response = self.client.get(self.delete_url)

        self.assertContains(response, "Archive product")
        self.assertContains(response, "mark it inactive")

    def test_product_without_history_is_deleted(self):
        response = self.client.post(self.delete_url)

        self.assertRedirects(response, reverse("inventory:product_list"))
        self.assertFalse(Product.objects.filter(pk=self.product.pk).exists())


class InventoryCatalogTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username="catalog-admin", password="catalog-password",
        )
        self.client.force_login(self.user)

    def test_category_brand_and_unit_pages_are_available(self):
        for name in ("category_list", "brand_list", "unit_list"):
            response = self.client.get(reverse(f"inventory:{name}"))
            self.assertEqual(response.status_code, 200)

    def test_can_create_brand_and_unit_catalog_entries(self):
        response = self.client.post(reverse("inventory:category_create"), {"name": "Paper"})
        self.assertRedirects(response, reverse("inventory:category_list"))
        self.assertTrue(Category.objects.filter(name="Paper", is_active=True).exists())

        response = self.client.post(reverse("inventory:brand_create"), {"name": "Acme"})
        self.assertRedirects(response, reverse("inventory:brand_list"))
        brand = Brand.objects.get(name="Acme")
        self.assertTrue(brand.is_active)

        response = self.client.post(reverse("inventory:unit_create"), {
            "code": "tray",
            "name": "Tray",
            "allow_fraction": "",
        })
        self.assertRedirects(response, reverse("inventory:unit_list"))
        unit = Unit.objects.get(code="tray")
        self.assertFalse(unit.allow_fraction)

    def test_unit_fraction_setting_controls_purchase_quantities(self):
        from purchase.services import PurchaseError, quantity

        with self.assertRaises(PurchaseError):
            quantity("1.5", "pcs", "Pieces")

        Unit.objects.create(code="roll", name="Roll", allow_fraction=True)
        self.assertEqual(quantity("1.5", "roll", "Roll"), Decimal("1.500"))

    def test_products_can_use_managed_brand_and_unit(self):
        brand = Brand.objects.create(name="Acme")
        unit = Unit.objects.create(code="roll", name="Roll", allow_fraction=True)
        form = ProductForm(data={
            "name": "Wrapping paper",
            "brand": brand.pk,
            "unit": unit.code,
            "cost_price": "1",
            "sale_price": "2",
            "mrp": "2",
            "gst_rate": "0",
            "hsn_code": "",
            "min_stock": "0",
            "is_active": "on",
        })

        self.assertTrue(form.is_valid(), form.errors)
        product = form.save()
        self.assertEqual(product.brand, brand)
        self.assertEqual(product.unit, "roll")

    def test_in_use_catalog_items_cannot_be_deleted(self):
        category = Category.objects.create(name="Paper")
        Product.objects.create(name="Wrapping paper", category=category)

        response = self.client.post(reverse("inventory:category_delete", args=[category.pk]))

        self.assertRedirects(response, reverse("inventory:category_list"))
        self.assertTrue(Category.objects.filter(pk=category.pk).exists())
        self.assertTrue(Category.objects.get(pk=category.pk).is_active)
