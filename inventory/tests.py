import tempfile
from pathlib import Path

from django.core.files.storage import default_storage
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import User

from .forms import ProductForm
from .models import Product
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

    def test_product_creation_generates_and_saves_barcode_and_image(self):
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
            self.assertRegex(product.barcode, r"^\d{12}$")
            self.assertTrue(default_storage.exists(product.barcode_image.name))

    def test_product_edit_preserves_existing_barcode_image(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            product = Product.objects.create(name="Before edit", barcode="EDIT-123")
            product.barcode_image.name = create_barcode_image(product.barcode)
            product.save(update_fields=["barcode_image"])
            image_name = product.barcode_image.name

            self.client.get(reverse("inventory:product_update", args=[product.pk]))
            response = self.client.post(
                reverse("inventory:product_update", args=[product.pk]),
                {
                    "name": "After edit",
                    "category": "",
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
            self.assertEqual(product.barcode_image.name, image_name)
            self.assertTrue(default_storage.exists(image_name))
