from decimal import Decimal
import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.storage import default_storage
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils.datastructures import MultiValueDict

from accounts.models import User

from .forms import CategoryForm, ProductForm
from .models import Brand, Category, Product, ProductImage, StockMovement, Unit
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

    def test_category_create_supports_main_and_nested_subcategories(self):
        main = Category.objects.create(name="Resin Material")
        create_url = reverse("inventory:category_create")

        response = self.client.get(create_url, {"parent": main.pk})
        self.assertContains(response, "New subcategory")
        self.assertEqual(int(response.context["form"]["parent"].value()), main.pk)

        response = self.client.post(
            create_url,
            {"name": "Resin Moulds", "parent": main.pk},
        )

        self.assertRedirects(response, reverse("inventory:category_list"))
        child = Category.objects.get(name="Resin Moulds")
        self.assertEqual(child.parent, main)
        self.assertEqual(str(child), "Resin Material / Resin Moulds")

    def test_category_list_and_product_tree_show_subcategories_nested(self):
        main = Category.objects.create(name="Art Material")
        child = Category.objects.create(name="Brushes", parent=main)

        response = self.client.get(reverse("inventory:category_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [(item.name, item.category_depth) for item in response.context["items"]],
            [("Art Material", 0), ("Brushes", 1)],
        )
        self.assertContains(response, "Add subcategory")
        product_response = self.client.get(reverse("inventory:product_list"))
        self.assertContains(product_response, "Brushes")
        self.assertContains(product_response, 'aria-label="Toggle Art Material subcategories"')
        self.assertContains(product_response, 'id="category-children-')
        self.assertContains(product_response, "category-children-inner")
        self.assertContains(product_response, "category-chevron")
        self.assertContains(product_response, "category-toggle")
        child_row = next(c for c in product_response.context["categories"] if c.pk == child.pk)
        self.assertEqual(child_row.category_depth, 1)

        selected_response = self.client.get(reverse("inventory:product_list"), {"group": child.pk})
        self.assertTrue(selected_response.context["category_tree"][0].tree_open)

    def test_category_list_search_filters_by_name(self):
        Category.objects.create(name="Resin Material")
        Category.objects.create(name="Resin Flowers")
        Category.objects.create(name="Paper")

        response = self.client.get(reverse("inventory:category_list"), {"q": "resin"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item.name for item in response.context["items"]], ["Resin Material", "Resin Flowers"])
        self.assertContains(response, "Search categories")

    def test_parent_category_cannot_be_deleted_while_it_has_subcategories(self):
        main = Category.objects.create(name="Craft Material")
        Category.objects.create(name="Paper", parent=main)

        response = self.client.post(reverse("inventory:category_delete", args=[main.pk]))

        self.assertRedirects(response, reverse("inventory:category_list"))
        self.assertTrue(Category.objects.filter(pk=main.pk).exists())

    def test_selecting_a_nested_category_still_filters_products_by_category(self):
        main = Category.objects.create(name="Resin Material")
        child = Category.objects.create(name="Resin Moulds", parent=main)
        Product.objects.create(name="Main item", category=main)
        Product.objects.create(name="Mould item", category=child)

        response = self.client.get(reverse("inventory:product_list"), {"group": child.pk})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mould item")
        self.assertNotContains(response, "Main item")
        self.assertTrue(response.context["category_tree"][0].tree_open)

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

    def test_product_form_adds_multiple_gallery_images(self):
        files = MultiValueDict({
            "gallery_images": [
                SimpleUploadedFile("front.png", b"front", content_type="image/png"),
                SimpleUploadedFile("back.png", b"back", content_type="image/png"),
            ]
        })
        form = ProductForm(
            data={
                "name": "Illustrated product",
                "unit": "pcs",
                "cost_price": "0",
                "sale_price": "0",
                "mrp": "0",
                "gst_rate": "0",
                "min_stock": "0",
                "dimensions": "",
                "tags_text": "",
            },
            files=files,
        )

        self.assertTrue(form.is_valid(), form.errors)
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            product = form.save()
            self.assertEqual(
                list(product.images.values_list("image", flat=True)),
                ["products/gallery/front.png", "products/gallery/back.png"],
            )

    def test_category_parent_cannot_create_a_cycle(self):
        parent = Category.objects.create(name="Parent")
        child = Category.objects.create(name="Child", parent=parent)
        form = CategoryForm(
            data={"name": "Parent", "parent": child.pk},
            instance=parent,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("parent", form.errors)

    def test_in_use_catalog_items_cannot_be_deleted(self):
        category = Category.objects.create(name="Paper")
        Product.objects.create(name="Wrapping paper", category=category)

        response = self.client.post(reverse("inventory:category_delete", args=[category.pk]))

        self.assertRedirects(response, reverse("inventory:category_list"))
        self.assertTrue(Category.objects.filter(pk=category.pk).exists())
        self.assertTrue(Category.objects.get(pk=category.pk).is_active)


class ProductImportTests(TestCase):
    def setUp(self):
        user = User.objects.create_superuser(username="import-admin", password="test-password")
        self.client.force_login(user)
        self.url = reverse("inventory:product_import")
        self.csv = (
            "Code,Name,Group,Brand,Barcode,Cost,Sale price,MRP,GST %,Stock\n"
            "ART-500,Resin mould,Silicone Mould,Art Co,123456,50,80,100,18,12.5\n"
        )

    def _upload(self, content=None):
        return self.client.post(self.url, {
            "action": "upload",
            "file": SimpleUploadedFile(
                "products.csv",
                (content or self.csv).encode("utf-8"),
                content_type="text/csv",
            ),
        })

    def test_import_button_opens_csv_mapping_workflow(self):
        response = self.client.get(reverse("inventory:product_list"))

        self.assertContains(response, reverse("inventory:product_import"))
        upload_response = self.client.get(self.url)
        self.assertContains(upload_response, "Choose a CSV file")

    def test_csv_import_maps_columns_creates_product_and_stock_movement(self):
        mapping_response = self._upload()

        self.assertEqual(mapping_response.status_code, 200)
        self.assertContains(mapping_response, "Map CSV fields to products")
        self.assertContains(mapping_response, "GST %")
        stage = mapping_response.context["stage"]
        response = self.client.post(self.url, {
            "action": "import",
            "token": stage["token"],
            "matching_behavior": "update",
            **{f"map_{index}": value for index, value in enumerate(
                ("code", "name", "category", "brand", "barcode", "cost_price", "sale_price", "mrp", "gst_rate", "stock")
            )},
        })

        product = Product.objects.get(code="ART-500")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["result"]["created"], 1)
        self.assertEqual(response.context["result"]["failed"], 0)
        self.assertEqual(product.category.name, "Silicone Mould")
        self.assertEqual(product.brand.name, "Art Co")
        self.assertEqual(product.cost_price, Decimal("50.00"))
        self.assertEqual(product.gst_rate, Decimal("18.00"))
        self.assertEqual(product.movements.get().quantity, Decimal("12.500"))
        self.assertEqual(product.movements.get().movement_type, StockMovement.Type.OPENING)

    def test_import_can_update_a_matching_product_and_reconcile_stock_ledger(self):
        product = Product.objects.create(name="Old", code="ART-500", sale_price="20", mrp="25")
        StockMovement.objects.create(
            product=product,
            quantity=4,
            movement_type=StockMovement.Type.OPENING,
        )
        stage = self._upload().context["stage"]
        response = self.client.post(self.url, {
            "action": "import",
            "token": stage["token"],
            "matching_behavior": "update",
            **{f"map_{index}": value for index, value in enumerate(
                ("code", "name", "category", "brand", "barcode", "cost_price", "sale_price", "mrp", "gst_rate", "stock")
            )},
        })

        product.refresh_from_db()
        self.assertEqual(response.context["result"]["updated"], 1)
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(product.name, "Resin mould")
        self.assertEqual(product.movements.count(), 2)
        self.assertEqual(product.movements.order_by("-id").first().quantity, Decimal("8.500"))

    def test_import_can_skip_existing_products(self):
        Product.objects.create(name="Existing", code="ART-500", sale_price="20", mrp="25")
        stage = self._upload().context["stage"]
        response = self.client.post(self.url, {
            "action": "import",
            "token": stage["token"],
            "matching_behavior": "skip",
            **{f"map_{index}": value for index, value in enumerate(
                ("code", "name", "category", "brand", "barcode", "cost_price", "sale_price", "mrp", "gst_rate", "stock")
            )},
        })

        self.assertEqual(response.context["result"]["skipped"], 1)
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(Product.objects.get().name, "Existing")

    def test_invalid_product_row_is_reported_without_creating_product(self):
        stage = self._upload(self.csv.replace(",80,100,", ",120,100,")).context["stage"]
        response = self.client.post(self.url, {
            "action": "import",
            "token": stage["token"],
            "matching_behavior": "update",
            **{f"map_{index}": value for index, value in enumerate(
                ("code", "name", "category", "brand", "barcode", "cost_price", "sale_price", "mrp", "gst_rate", "stock")
            )},
        })

        self.assertEqual(response.context["result"]["failed"], 1)
        self.assertFalse(Product.objects.exists())

    def test_import_requires_name_mapping_and_keeps_mapping_step_open(self):
        stage = self._upload().context["stage"]
        response = self.client.post(self.url, {
            "action": "import",
            "token": stage["token"],
            "matching_behavior": "update",
            "map_0": "code",
        })

        self.assertContains(response, "Map a CSV column to Name")
        self.assertIn("stage", response.context)
        self.assertFalse(Product.objects.exists())


class PriceTagLayoutTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="tag-user")
        self.client.force_login(user)

    def test_label_width_is_a_manual_field(self):
        response = self.client.get(reverse("inventory:price_tags"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Label width (mm, manual)")
        self.assertContains(response, 'id="lw" type="number"')
        self.assertContains(response, "const lw = num('lw')")
