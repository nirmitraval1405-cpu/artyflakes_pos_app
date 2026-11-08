import json
import tempfile
from io import BytesIO
from decimal import Decimal

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.models import User
from inventory.models import Product, StockMovement
from pos.models import Sale

from .forms import StoreSettingsForm
from .models import StoreSettings


def valid_data(**over):
    data = {
        "name": "Craft Hut", "tagline": "Art and craft",
        "street_name": "12 Main Road", "building_number": "12",
        "additional_street_name": "Near the market", "plot_identification": "Plot 8",
        "district": "Ahmedabad", "postal_code": "380001", "city": "Ahmedabad",
        "state_province": "Gujarat", "country": "India",
        "phone": "9876543210", "email": "shop@example.com", "gstin": "24abcde1234f1z5",
        "bank_account_number": "123456789012", "bank_details": "Craft Hut Bank\nIFSC: ABCD0123456",
        "receipt_footer": "Thank you", "cashier_max_discount_percent": "10",
    }
    data.update(over)
    return data


class StoreSettingsModelTests(TestCase):
    def test_load_seeds_from_settings_once(self):
        store = {"name": "Craft Hut", "address": "12 Main Road", "phone": "9876543210",
                 "gstin": "24ABCDE1234F1Z5", "footer": "Bye"}
        with override_settings(STORE=store, POS_CASHIER_MAX_DISCOUNT_PERCENT=15):
            s = StoreSettings.load()
        self.assertEqual(s.name, "Craft Hut")
        self.assertEqual(s.receipt_footer, "Bye")
        self.assertEqual(s.cashier_max_discount_percent, Decimal("15.00"))
        with override_settings(STORE={"name": "Other"}):
            self.assertEqual(StoreSettings.load().name, "Craft Hut")      # settings.py is ignored after the first run

    def test_existing_address_is_kept_until_structured_address_is_entered(self):
        store = StoreSettings.load()
        store.address = "12 Main Road\nAhmedabad"
        store.save()
        self.assertEqual(store.as_store_info()["address"], "12 Main Road\nAhmedabad")

    def test_bad_first_run_values_do_not_crash(self):
        with override_settings(STORE={"gstin": "bad", "phone": "x"}, POS_CASHIER_MAX_DISCOUNT_PERCENT=500):
            s = StoreSettings.load()
        self.assertEqual(s.gstin, "")
        self.assertEqual(s.phone, "")
        self.assertEqual(s.cashier_max_discount_percent, Decimal("10.00"))

    def test_there_is_only_ever_one_row(self):
        s = StoreSettings.load()
        s.pk = 9
        s.name = "Z"
        s.save()
        self.assertEqual(StoreSettings.objects.count(), 1)
        self.assertEqual(StoreSettings.objects.get().pk, 1)

    def test_it_cannot_be_deleted(self):
        with self.assertRaises(ValidationError):
            StoreSettings.load().delete()

    def test_admin_site_is_read_only(self):
        ma = admin.site._registry[StoreSettings]
        self.assertFalse(ma.has_add_permission(None))
        self.assertFalse(ma.has_change_permission(None))
        self.assertFalse(ma.has_delete_permission(None))


class StoreSettingsFormTests(TestCase):
    def test_valid_data_saves_gstin_in_upper_case(self):
        form = StoreSettingsForm(valid_data(), instance=StoreSettings.load())
        self.assertTrue(form.is_valid(), form.errors)
        store = form.save()
        self.assertEqual(store.gstin, "24ABCDE1234F1Z5")
        self.assertEqual(
            store.as_store_info()["address"],
            "12 Main Road\n12\nNear the market\nPlot 8\nAhmedabad\n380001\nAhmedabad\nGujarat\nIndia",
        )
        self.assertEqual(store.as_store_info()["bank_account_number"], "123456789012")

    def test_bad_values_are_rejected(self):
        for field, value in (
            ("cashier_max_discount_percent", "101"), ("cashier_max_discount_percent", "-1"),
            ("cashier_max_discount_percent", "10.555"), ("cashier_max_discount_percent", "abc"),
            ("gstin", "123"), ("phone", "abc"), ("name", "   "),
        ):
            form = StoreSettingsForm(valid_data(**{field: value}), instance=StoreSettings.load())
            self.assertFalse(form.is_valid(), f"{field}={value!r} was accepted")

    def test_logo_is_saved_and_can_be_cleared(self):
        image_buffer = BytesIO()
        Image.new("RGB", (1, 1), color="white").save(image_buffer, format="PNG")
        logo = SimpleUploadedFile(
            "store-logo.png",
            image_buffer.getvalue(),
            content_type="image/png",
        )
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            form = StoreSettingsForm(valid_data(), {"logo": logo}, instance=StoreSettings.load())
            self.assertTrue(form.is_valid(), form.errors)
            store = form.save()
            self.assertEqual(store.logo.name, "store/store-logo.png")
            self.assertTrue(store.as_store_info()["logo"].endswith("/store/store-logo.png"))

            clear_form = StoreSettingsForm(
                valid_data(**{"logo-clear": "on"}), instance=store
            )
            self.assertTrue(clear_form.is_valid(), clear_form.errors)
            self.assertFalse(clear_form.save().logo)


class StoreSettingsPageTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("sc_boss", password="pw12345!", role="admin")
        User.objects.create_user("sc_cash", password="pw12345!", role="cashier")
        self.url = reverse("storeconfig:store")

    def test_cashier_cannot_open_or_post(self):
        self.client.login(username="sc_cash", password="pw12345!")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, valid_data()).status_code, 403)
        self.assertNotEqual(StoreSettings.load().name, "Craft Hut")

    def test_admin_can_save(self):
        self.client.login(username="sc_boss", password="pw12345!")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.assertContains(response, "Building number")
        self.assertContains(response, "Bank account")
        self.assertContains(response, "Logo")
        self.assertEqual(self.client.post(self.url, valid_data()).status_code, 302)
        s = StoreSettings.load()
        self.assertEqual(s.name, "Craft Hut")
        self.assertEqual(s.gstin, "24ABCDE1234F1Z5")
        self.assertEqual(s.updated_by, self.admin)

    def test_invalid_values_show_errors_and_change_nothing(self):
        self.client.login(username="sc_boss", password="pw12345!")
        before = StoreSettings.load().cashier_max_discount_percent
        response = self.client.post(self.url, valid_data(cashier_max_discount_percent="101"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(StoreSettings.load().cashier_max_discount_percent, before)

    def test_sidebar_link_is_for_admins_only(self):
        self.client.login(username="sc_cash", password="pw12345!")
        self.assertNotContains(self.client.get(reverse("pos:sale_list")), "Store Settings")
        self.client.logout()
        self.client.login(username="sc_boss", password="pw12345!")
        self.assertContains(self.client.get(reverse("pos:sale_list")), "Store Settings")

    def test_store_name_reaches_every_page_and_login_still_works(self):
        s = StoreSettings.load()
        s.name = "Craft Hut"
        s.save()
        self.client.login(username="sc_boss", password="pw12345!")
        self.assertEqual(self.client.get(reverse("pos:sale_list")).context["store_name"], "Craft Hut")
        self.client.logout()
        self.assertEqual(self.client.get(reverse("accounts:login")).status_code, 200)


class SnapshotAndLimitTests(TestCase):
    def setUp(self):
        User.objects.create_user("sc_c", password="pw12345!", role="cashier")
        User.objects.create_user("sc_a", password="pw12345!", role="admin")
        self.product = Product.objects.create(name="Resin", sale_price=Decimal("250.00"), mrp=Decimal("300.00"))
        StockMovement.objects.create(product=self.product, quantity=50, movement_type="opening")
        self.client.login(username="sc_c", password="pw12345!")

    def sell(self, discount=None, cash="500.00", client=None):
        item = {"product_id": self.product.pk, "quantity": "2"}
        if discount:
            item["discount"] = {"type": "percent", "value": discount}
        body = {"items": [item], "payments": [{"method": "cash", "amount": cash}]}
        return (client or self.client).post(reverse("pos:checkout"), data=json.dumps(body), content_type="application/json")

    def change(self, **fields):
        s = StoreSettings.load()
        for key, value in fields.items():
            setattr(s, key, value)
        s.save()

    def test_old_bills_keep_the_details_they_were_made_with(self):
        self.change(name="Shop One")
        first = Sale.objects.get(invoice_no=self.sell().json()["invoice_no"])
        self.change(name="Shop Two")
        second = Sale.objects.get(invoice_no=self.sell().json()["invoice_no"])

        old = self.client.get(reverse("pos:receipt", args=[first.pk]))
        self.assertContains(old, "Shop One")
        self.assertNotContains(old, "Shop Two")
        self.assertContains(self.client.get(reverse("pos:invoice", args=[second.pk])), "Shop Two")

        Sale.objects.filter(pk=first.pk).update(store_info=None)        # a bill from before this feature existed
        self.assertContains(self.client.get(reverse("pos:receipt", args=[first.pk])), "Shop Two")

    def test_company_and_bank_details_are_saved_on_new_invoice(self):
        self.change(
            name="Craft Hut",
            street_name="12 Main Road",
            city="Ahmedabad",
            country="India",
            bank_account_number="123456789012",
            bank_details="Craft Hut Bank\nIFSC: ABCD0123456",
        )
        sale = Sale.objects.get(invoice_no=self.sell().json()["invoice_no"])
        response = self.client.get(reverse("pos:invoice", args=[sale.pk]))
        self.assertContains(response, "12 Main Road")
        self.assertContains(response, "Ahmedabad")
        self.assertContains(response, "123456789012")
        self.assertContains(response, "ABCD0123456")

    def test_discount_limit_follows_the_settings_page(self):
        self.change(cashier_max_discount_percent=Decimal("10"))
        self.assertEqual(self.sell(discount="15").status_code, 400)
        self.change(cashier_max_discount_percent=Decimal("20"))
        self.assertEqual(self.sell(discount="15").status_code, 200)
        self.change(cashier_max_discount_percent=Decimal("5"))
        self.assertEqual(self.sell(discount="15").status_code, 400)

    def test_admins_are_never_limited(self):
        self.change(cashier_max_discount_percent=Decimal("5"))
        boss = Client()
        boss.login(username="sc_a", password="pw12345!")
        self.assertEqual(self.sell(discount="50", cash="250.00", client=boss).status_code, 200)