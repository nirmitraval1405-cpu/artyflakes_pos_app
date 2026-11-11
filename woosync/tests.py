import base64
import hashlib
import hmac
import json
import socket
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock
from urllib import error

from django.contrib import admin
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from inventory.models import Product, StockMovement
from inventory.stock import current_stock

from .client import WooClient, WooError
from .models import (
    ProductSyncConflict, ProductSyncJob, SyncRun, SyncState, WebOrder, WebOrderItem, WooLink,
)
from .sync import (MASS_ZERO_LIMIT, InvalidOrderPayload, SyncBusy, SyncOff, apply_order, match_products,
                   planned_pushes, reconcile_stock, run_sync)
from .products import process_product_jobs, sync_product_from_woo

UTC = dt_timezone.utc


def line(lid=1, product_id=55, sku="RES-1", qty=2, total="500.00"):
    return {"id": lid, "product_id": product_id, "variation_id": 0, "name": "Resin", "sku": sku, "quantity": qty, "total": total}


def order(woo_id=101, status="processing", lines=None, modified="2026-11-10T10:00:00", created="2026-11-10T09:00:00"):
    return {
        "id": woo_id, "number": str(woo_id), "status": status,
        "date_created_gmt": created, "date_modified_gmt": modified,
        "date_paid_gmt": None if status == "pending" else "2026-11-10T09:01:00",
        "total": "500.00", "currency": "INR", "payment_method_title": "Razorpay", "customer_note": "",
        "billing": {"first_name": "Asha", "last_name": "Shah", "email": "a@example.com", "phone": "9876543210",
                    "address_1": "12 Main Road", "city": "Ahmedabad", "postcode": "380001", "country": "IN"},
        "shipping": {"first_name": "Asha", "last_name": "Shah", "address_1": "12 Main Road", "city": "Ahmedabad"},
        "line_items": [line()] if lines is None else lines,
    }


class FakeClient:
    """Stands in for the website. Nothing here touches the network."""

    def __init__(self, orders=None, products=None, fail=False):
        self.orders, self.products, self.fail = orders or [], products or [], fail
        self.posted, self.order_params, self.errors = [], None, {}

    def get_all(self, path, params=None):
        if self.fail:
            raise WooError("down")
        if path == "orders":
            self.order_params = params
            return list(self.orders)
        return list(self.products)

    def post(self, path, body):
        if self.fail:
            raise WooError("down")
        self.posted.append((path, body))
        out = []
        for u in body["update"]:
            if u["id"] in self.errors:
                out.append({"id": u["id"], "error": {"message": self.errors[u["id"]]}})
            else:
                out.append({"id": u["id"], "stock_quantity": u["stock_quantity"]})
        return {"update": out}


class FakeProductClient:
    def __init__(self):
        self.products = {}
        self.next_id = 500
        self.posted = []

    def get_all(self, path, params=None):
        if path == "products/categories" or path == "products/tags":
            return []
        rows = list(self.products.values())
        sku = (params or {}).get("sku")
        return [row for row in rows if not sku or row.get("sku") == sku]

    def get_page(self, path, params=None):
        product_id = int(path.rsplit("/", 1)[1])
        return dict(self.products[product_id]), {}

    def post(self, path, body):
        self.posted.append((path, body))
        if path != "products":
            self.next_id += 1
            return {"id": self.next_id, **body}
        self.next_id += 1
        product = {
            "id": self.next_id,
            "type": "simple",
            "date_modified_gmt": "2026-11-11T00:00:00",
            "categories": [],
            "tags": [],
            "images": [],
        }
        product.update(body)
        self.products[product["id"]] = product
        return dict(product)

    def put(self, path, body):
        product_id = int(path.rsplit("/", 1)[1])
        self.products[product_id].update(body)
        return dict(self.products[product_id])


class FakeResponse:
    def __init__(self, body, headers=None):
        self._body, self.headers = body, headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Base(TestCase):
    def setUp(self):
        self.product = Product.objects.create(name="Resin", code="RES-1", sale_price=Decimal("250.00"))
        StockMovement.objects.create(product=self.product, quantity=10, movement_type="opening")

    def stock(self, product=None):
        return current_stock(product or self.product)

    def enable(self, **fields):
        state = SyncState.load()
        state.is_enabled = True
        for key, value in fields.items():
            setattr(state, key, value)
        state.save()
        return state

    def link(self, **kw):
        values = {"sell_online": True, "woo_product_id": 55, "woo_type": "simple"}
        values.update(kw)
        return WooLink.objects.create(product=self.product, **values)


class ProductSyncTests(Base):
    def woo_product(self, **overrides):
        data = {
            "id": 55,
            "type": "simple",
            "name": "Resin",
            "sku": "RES-1",
            "status": "publish",
            "regular_price": "300.00",
            "sale_price": "250.00",
            "description": "Product description",
            "short_description": "Short description",
            "tax_class": "standard",
            "weight": "0.5",
            "dimensions": {"length": "10", "width": "8", "height": "3"},
            "attributes": [],
            "categories": [],
            "tags": [],
            "images": [],
            "manage_stock": True,
            "stock_quantity": 7,
            "date_modified_gmt": "2026-11-11T00:00:00",
        }
        data.update(overrides)
        return data

    def test_importing_a_simple_product_is_repeatable_and_keeps_pos_gst(self):
        self.product.gst_rate = Decimal("18")
        self.product.save(update_fields=["gst_rate"])

        self.assertEqual(sync_product_from_woo(self.woo_product()), "updated")
        self.assertEqual(sync_product_from_woo(self.woo_product()), "updated")

        self.product.refresh_from_db()
        self.assertEqual(self.product.mrp, Decimal("300.00"))
        self.assertEqual(self.product.sale_price, Decimal("250.00"))
        self.assertEqual(self.product.gst_rate, Decimal("18"))
        self.assertEqual(self.stock(), Decimal("7"))
        self.assertEqual(
            StockMovement.objects.filter(source=StockMovement.Source.WOOCOMMERCE).count(), 1
        )
        self.assertEqual(WooLink.objects.get(product=self.product).woo_product_id, 55)

    def test_variable_products_are_queued_for_review_not_imported(self):
        result = sync_product_from_woo(
            self.woo_product(id=56, type="variable", sku="VAR-1")
        )

        self.assertEqual(result, "review")
        self.assertFalse(Product.objects.filter(code="VAR-1").exists())
        job = ProductSyncJob.objects.get(woo_product_id=56)
        self.assertEqual(job.status, ProductSyncJob.Status.REVIEW)

    def test_pos_product_create_job_publishes_a_simple_product(self):
        WooLink.objects.create(product=self.product, sell_online=True)
        job = ProductSyncJob.objects.create(
            product=self.product,
            direction="pos",
            event_type="created",
            idempotency_key="test-pos-create",
            payload={"fields": ["name", "sku", "regular_price", "sale_price"]},
        )
        client = FakeProductClient()

        counts = process_product_jobs(client)

        job.refresh_from_db()
        link = WooLink.objects.get(product=self.product)
        self.assertEqual(counts["processed"], 1)
        self.assertEqual(job.status, ProductSyncJob.Status.DONE)
        self.assertEqual(link.woo_product_id, 501)
        self.assertEqual(client.products[501]["sku"], self.product.code)

    def test_two_sided_edits_create_a_review_conflict(self):
        link = self.link(
            last_synced_data={
                "name": "Resin",
                "sku": "RES-1",
                "regular_price": "250.00",
                "sale_price": "",
                "description": "",
                "short_description": "",
                "status": "publish",
                "tax_class": "",
                "weight": "",
                "dimensions": {},
                "attributes": [],
            }
        )
        self.product.name = "POS Resin"
        self.product.save(update_fields=["name"])
        data = self.woo_product(name="Website Resin", stock_quantity=None)

        self.assertEqual(sync_product_from_woo(data), "updated")

        conflict = ProductSyncConflict.objects.get(product=self.product, field_name="name")
        self.assertEqual(conflict.pos_value, "POS Resin")
        self.assertEqual(conflict.woo_value, "Website Resin")
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, "POS Resin")
        link.refresh_from_db()
        self.assertTrue(link.needs_review)


class OrderTests(Base):
    def test_a_processing_order_takes_stock_out(self):
        self.assertEqual(apply_order(order()), "new")
        self.assertEqual(self.stock(), Decimal("8"))
        move = StockMovement.objects.get(movement_type="web_order")
        self.assertEqual((move.reference, move.quantity), ("WC-101", Decimal("-2")))
        item = WebOrder.objects.get().items.get()
        self.assertEqual((item.product, item.applied_qty), (self.product, Decimal("2")))

    def test_running_the_same_order_again_changes_nothing(self):
        apply_order(order())
        self.assertEqual(apply_order(order()), "updated")
        self.assertEqual(self.stock(), Decimal("8"))
        self.assertEqual(StockMovement.objects.filter(movement_type="web_order").count(), 1)

    def test_only_paid_or_accepted_statuses_take_stock(self):
        for i, status in enumerate(("processing", "on-hold", "completed")):
            apply_order(order(woo_id=100 + i, status=status))
        self.assertEqual(self.stock(), Decimal("4"))
        for i, status in enumerate(("pending", "cancelled", "failed", "refunded", "trash")):
            apply_order(order(woo_id=200 + i, status=status))
        self.assertEqual(self.stock(), Decimal("4"))

    def test_a_pending_order_takes_stock_once_it_is_accepted(self):
        apply_order(order(status="pending"))
        self.assertEqual(self.stock(), Decimal("10"))
        apply_order(order(status="processing"))
        self.assertEqual(self.stock(), Decimal("8"))

    def test_cancelling_gives_the_stock_back_and_reviving_takes_it_again(self):
        apply_order(order(status="processing"))
        apply_order(order(status="cancelled"))
        self.assertEqual(self.stock(), Decimal("10"))
        self.assertTrue(StockMovement.objects.filter(movement_type="web_order_return").exists())
        apply_order(order(status="processing"))
        self.assertEqual(self.stock(), Decimal("8"))
        self.assertEqual(WebOrder.objects.count(), 1)

    def test_editing_an_order_on_the_website_corrects_the_stock(self):
        apply_order(order(lines=[line(qty=2)]))
        apply_order(order(lines=[line(qty=3)]))
        self.assertEqual(self.stock(), Decimal("7"))
        apply_order(order(lines=[line(qty=1)]))
        self.assertEqual(self.stock(), Decimal("9"))
        apply_order(order(lines=[]))
        self.assertEqual(self.stock(), Decimal("10"))

    def test_an_unmatched_item_is_flagged_and_fixed_later(self):
        apply_order(order(lines=[line(sku="NOPE", product_id=999)]))
        self.assertIsNone(WebOrder.objects.get().items.get().product)
        self.assertEqual(self.stock(), Decimal("10"))

        other = Product.objects.create(name="Other", code="NOPE", sale_price=Decimal("10"))
        StockMovement.objects.create(product=other, quantity=5, movement_type="opening")
        reconcile_stock(WebOrder.objects.get())
        self.assertEqual(self.stock(other), Decimal("3"))

    def test_the_sku_match_ignores_capital_letters(self):
        apply_order(order(lines=[line(sku="res-1", product_id=999)]))
        self.assertEqual(WebOrder.objects.get().items.get().product, self.product)

    def test_a_linked_website_product_id_is_matched_first(self):
        self.link(sell_online=False)
        apply_order(order(lines=[line(sku="", product_id=55)]))
        self.assertEqual(WebOrder.objects.get().items.get().product, self.product)

    def test_products_without_stock_tracking_take_no_stock(self):
        self.product.track_stock = False
        self.product.save()
        apply_order(order())
        self.assertEqual(StockMovement.objects.filter(movement_type="web_order").count(), 0)

    def test_orders_placed_before_the_sync_started_are_ignored(self):
        state = SyncState.load()
        state.orders_since = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
        state.save()
        self.assertIsNone(apply_order(order(woo_id=1, created="2026-11-10T09:00:00")))
        self.assertEqual(WebOrder.objects.count(), 0)
        self.assertEqual(apply_order(order(woo_id=2, created="2026-11-10T10:00:00")), "new")

    def test_an_order_we_already_have_keeps_updating_after_the_start_date(self):
        apply_order(order(woo_id=3, created="2026-11-10T09:00:00", status="pending"))
        state = SyncState.load()
        state.orders_since = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
        state.save()
        self.assertEqual(apply_order(order(woo_id=3, created="2026-11-10T09:00:00", status="processing")), "updated")
        self.assertEqual(self.stock(), Decimal("8"))

    def test_drafts_and_nonsense_are_ignored(self):
        self.assertIsNone(apply_order(order(status="checkout-draft")))
        self.assertIsNone(apply_order({"id": "x"}))
        self.assertIsNone(apply_order("nope"))
        self.assertEqual(WebOrder.objects.count(), 0)

    def test_order_details_are_copied(self):
        apply_order(order())
        o = WebOrder.objects.get()
        self.assertEqual((o.customer_name, o.phone, o.payment_method, o.total), ("Asha Shah", "9876543210", "Razorpay", Decimal("500.00")))
        self.assertTrue(o.is_paid)
        self.assertIn("12 Main Road", o.shipping_address)

    def test_accounting_values_are_preserved_for_later_conversion(self):
        payload = order()
        payload.update({
            "payment_method": "razorpay",
            "transaction_id": "txn-123",
            "prices_include_tax": False,
            "discount_total": "50.00",
            "discount_tax": "9.00",
            "shipping_total": "25.00",
            "shipping_tax": "4.50",
            "cart_tax": "72.00",
            "total_tax": "76.50",
            "tax_lines": [{"rate_id": 3, "label": "CGST", "tax_total": "38.25"}],
            "shipping_lines": [
                {"method_title": "Flat rate", "total": "25.00", "total_tax": "4.50", "taxes": []},
            ],
            "coupon_lines": [{"code": "SAVE", "discount": "50.00", "discount_tax": "9.00"}],
            "refunds": [{"id": 9, "reason": "Partial refund", "total": "-10.00"}],
        })
        payload["line_items"][0].update({
            "subtotal": "500.00",
            "subtotal_tax": "90.00",
            "total_tax": "81.00",
            "taxes": [{"id": 3, "total": "81.00", "subtotal": "90.00"}],
        })

        apply_order(payload)

        imported = WebOrder.objects.get()
        self.assertEqual(imported.payment_method_code, "razorpay")
        self.assertEqual(imported.transaction_id, "txn-123")
        self.assertEqual(imported.financial_data["prices_include_tax"], False)
        self.assertEqual(imported.financial_data["shipping_tax"], "4.50")
        self.assertEqual(imported.financial_data["line_items"][0]["taxes"][0]["total"], "81.00")
        self.assertEqual(imported.financial_data["shipping_lines"][0]["total"], "25.00")
        self.assertEqual(imported.financial_data["coupon_lines"][0]["code"], "SAVE")
        self.assertEqual(imported.financial_data["refunds"][0]["id"], 9)

    def test_malformed_accounting_rows_fail_without_creating_an_order(self):
        payload = order()
        payload["tax_lines"] = ["invalid"]

        with self.assertRaisesRegex(InvalidOrderPayload, "tax_lines must be a list of objects"):
            apply_order(payload)

        self.assertEqual(WebOrder.objects.count(), 0)
        self.assertEqual(self.stock(), Decimal("10"))


class RunSyncTests(Base):
    def test_a_switched_off_sync_does_nothing(self):
        with self.assertRaises(SyncOff):
            run_sync(client=FakeClient(orders=[order()]))
        self.assertEqual(WebOrder.objects.count(), 0)

    def test_orders_come_in_and_the_bookmark_moves(self):
        self.enable()
        client = FakeClient(orders=[order(modified="2026-11-10T10:00:00")])
        run = run_sync("manual", client=client)
        self.assertTrue(run.ok)
        self.assertEqual((run.orders_seen, run.orders_new, run.orders_updated), (1, 1, 0))
        state = SyncState.load()
        self.assertEqual(state.orders_cursor, datetime(2026, 11, 10, 10, 0, tzinfo=UTC))
        self.assertEqual(run.orders_failed, 0)
        self.assertTrue(state.last_ok)
        self.assertIsNone(state.running_since)
        self.assertNotIn("modified_after", client.order_params)

        second = FakeClient()
        run_sync(client=second)
        self.assertEqual(second.order_params["modified_after"], "2026-11-10T09:55:00")      # a 5-minute overlap

    def test_scheduled_management_command_runs_the_existing_sync(self):
        self.enable()
        client = FakeClient(orders=[order()])
        with mock.patch("woosync.sync.WooClient", return_value=client):
            call_command("sync_woo", stdout=mock.Mock())

        run = SyncRun.objects.get()
        self.assertEqual(run.trigger, "scheduled")
        self.assertTrue(run.ok)
        self.assertEqual(run.orders_new, 1)

    def test_orders_are_imported_before_stock_is_sent(self):
        self.link()
        self.enable()
        client = FakeClient(orders=[order()])                         # a web order for 2 units
        run_sync(client=client)
        update = client.posted[0][1]["update"][0]
        self.assertEqual(update, {"id": 55, "manage_stock": True, "stock_quantity": 8})

    def test_a_failed_order_blocks_the_stock_push_and_the_bookmark(self):
        self.link()
        self.enable()
        bad = order()
        bad["line_items"] = ["oops"]
        client = FakeClient(orders=[bad])
        with self.assertLogs("woosync", level="WARNING") as logs:
            run = run_sync(client=client)
        self.assertIn("line_items must be a list of objects", logs.output[0])
        self.assertNotIn("Traceback", logs.output[0])
        self.assertFalse(run.ok)
        self.assertEqual(run.orders_failed, 1)
        self.assertIn("1 orders failed to import", run.summary)
        self.assertEqual(client.posted, [])
        self.assertEqual(WebOrder.objects.count(), 0)
        self.assertIsNone(SyncState.load().orders_cursor)
        self.assertFalse(SyncState.load().last_ok)

    def test_a_website_error_is_recorded_and_nothing_is_sent(self):
        self.link()
        self.enable()
        client = FakeClient(fail=True)
        run = run_sync(client=client)
        self.assertFalse(run.ok)
        self.assertIn("down", run.summary)
        self.assertEqual(client.posted, [])

    def test_a_running_sync_blocks_another_but_a_dead_lock_does_not(self):
        self.enable(running_since=timezone.now())
        with self.assertRaises(SyncBusy):
            run_sync(client=FakeClient())
        self.enable(running_since=timezone.now() - timedelta(minutes=15))
        self.assertTrue(run_sync(client=FakeClient()).ok)


class PushTests(Base):
    def test_only_changed_stock_is_sent_and_it_is_sent_again_when_it_changes(self):
        link = self.link()
        self.enable()
        first = FakeClient()
        run_sync(client=first)
        self.assertEqual(first.posted[0][1]["update"][0]["stock_quantity"], 10)
        link.refresh_from_db()
        self.assertEqual(link.last_pushed_qty, 10)

        second = FakeClient()
        run_sync(client=second)
        self.assertEqual(second.posted, [])

        StockMovement.objects.create(product=self.product, quantity=-3, movement_type="sale")
        third = FakeClient()
        run_sync(client=third)
        self.assertEqual(third.posted[0][1]["update"][0]["stock_quantity"], 7)

    def test_unsuitable_products_are_not_sent(self):
        link = self.link(sell_online=False)
        self.assertEqual(planned_pushes(), [])
        link.sell_online, link.woo_type = True, "variable"
        link.save()
        self.assertEqual(planned_pushes(), [])
        link.woo_type = "simple"
        link.save()
        self.assertEqual(len(planned_pushes()), 1)
        self.product.track_stock = False
        self.product.save()
        self.assertEqual(planned_pushes(), [])

    def test_a_product_with_no_website_id_is_not_sent(self):
        self.link(woo_product_id=None)
        self.assertEqual(planned_pushes(), [])

    def test_fractions_round_down_the_holdback_applies_and_negative_becomes_zero(self):
        self.link()
        StockMovement.objects.create(product=self.product, quantity=Decimal("-7.1"), movement_type="damage")   # 2.9 left
        self.assertEqual([t for _, _, t in planned_pushes(0)], [2])
        self.assertEqual([t for _, _, t in planned_pushes(1)], [1])
        StockMovement.objects.create(product=self.product, quantity=Decimal("-10"), movement_type="damage")    # now -7.1
        self.assertEqual([t for _, _, t in planned_pushes(0)], [0])

    def test_an_inactive_product_is_sent_as_zero(self):
        self.link()
        self.product.is_active = False
        self.product.save()
        self.assertEqual([t for _, _, t in planned_pushes()], [0])

    def test_a_website_error_keeps_the_old_value_and_is_reported(self):
        link = self.link()
        self.enable()
        client = FakeClient()
        client.errors = {55: "No such product"}
        run = run_sync(client=client)
        link.refresh_from_db()
        self.assertIsNone(link.last_pushed_qty)
        self.assertFalse(run.ok)
        self.assertIn("Resin", run.summary)

    def test_a_scheduled_run_will_not_send_a_lot_of_zeros(self):
        for last in (5, None):
            WooLink.objects.all().delete()
            for i in range(MASS_ZERO_LIMIT + 2):
                p = Product.objects.create(name=f"P{last}-{i}", code=f"P{last}-{i}", sale_price=Decimal("10"))
                WooLink.objects.create(product=p, sell_online=True, woo_product_id=1000 + i, woo_type="simple", last_pushed_qty=last)
            self.enable()
            client = FakeClient()
            run = run_sync(client=client)
            self.assertEqual(client.posted, [])
            self.assertIn("NOT sent", run.summary)
            self.assertTrue(run.ok)

            forced = FakeClient()
            run_sync(client=forced, allow_mass_zero=True)
            self.assertEqual(len(forced.posted[0][1]["update"]), MASS_ZERO_LIMIT + 2)
            Product.objects.filter(code__startswith="P").delete()

    def test_big_lists_are_sent_in_batches_of_100(self):
        products = Product.objects.bulk_create([Product(name=f"B{i}", code=f"B-{i}", sale_price=Decimal("10")) for i in range(101)])
        WooLink.objects.bulk_create([WooLink(product=p, sell_online=True, woo_product_id=2000 + i, woo_type="simple") for i, p in enumerate(products)])
        StockMovement.objects.bulk_create([StockMovement(product=p, quantity=1, movement_type="opening") for p in products])
        self.enable()
        client = FakeClient()
        run_sync(client=client)
        self.assertEqual([len(body["update"]) for _, body in client.posted], [100, 1])


class MatchTests(Base):
    def test_matching_by_sku(self):
        variable = Product.objects.create(name="Kit", code="RES-2", sale_price=Decimal("5"))
        client = FakeClient(products=[
            {"id": 55, "sku": "res-1", "name": "Resin", "type": "simple"},
            {"id": 56, "sku": "", "name": "No sku", "type": "simple"},
            {"id": 57, "sku": "ZZZ", "name": "Unknown", "type": "simple"},
            {"id": 58, "sku": "RES-2", "name": "Kit", "type": "variable"},
        ])
        result = match_products(client)
        self.assertEqual((result["matched"], result["unsupported"]), (2, 1))
        self.assertEqual(result["no_sku"], ["No sku"])
        self.assertEqual(result["unknown_sku"], ["Unknown (ZZZ)"])
        link = WooLink.objects.get(product=self.product)
        self.assertEqual((link.woo_product_id, link.woo_type, link.sell_online), (55, "simple", False))
        self.assertEqual(WooLink.objects.get(product=variable).woo_type, "variable")

    def test_a_changed_website_id_forces_a_fresh_send_and_ids_stay_unique(self):
        variable = Product.objects.create(name="Kit", code="RES-2", sale_price=Decimal("5"))
        match_products(FakeClient(products=[
            {"id": 55, "sku": "RES-1", "name": "Resin", "type": "simple"},
            {"id": 58, "sku": "RES-2", "name": "Kit", "type": "simple"},
        ]))
        link = WooLink.objects.get(product=self.product)
        link.last_pushed_qty = 5
        link.save()

        match_products(FakeClient(products=[{"id": 99, "sku": "RES-1", "name": "Resin", "type": "simple"}]))
        link.refresh_from_db()
        self.assertEqual((link.woo_product_id, link.last_pushed_qty), (99, None))

        match_products(FakeClient(products=[{"id": 58, "sku": "RES-1", "name": "Resin", "type": "simple"}]))
        self.assertIsNone(WooLink.objects.get(product=variable).woo_product_id)       # 58 moved to Resin
        link.refresh_from_db()
        self.assertEqual(link.woo_product_id, 58)


class ClientTests(TestCase):
    def test_plain_http_and_missing_keys_are_refused(self):
        with self.assertRaises(WooError):
            WooClient("http://shop.test", "ck_a", "cs_b")
        with self.assertRaises(WooError):
            WooClient("https://shop.test", "", "")

    @override_settings(WOOSYNC={"URL": "https://shop.test", "KEY": "ck_key", "SECRET": "cs_secret"})
    def test_the_settings_are_used(self):
        self.assertEqual(WooClient().url, "https://shop.test")

    def test_login_header_and_paging(self):
        client = WooClient("https://shop.test/", "ck_key", "cs_secret")
        pages = [
            FakeResponse(json.dumps([{"id": 1}]).encode(), {"X-WP-TotalPages": "2"}),
            FakeResponse(json.dumps([{"id": 2}]).encode(), {"X-WP-TotalPages": "2"}),
        ]
        with mock.patch("woosync.client.request.urlopen", side_effect=pages) as op:
            result = client.get_all("orders", {"status": "any"})
        self.assertEqual([r["id"] for r in result], [1, 2])
        first = op.call_args_list[0].args[0]
        self.assertTrue(first.full_url.startswith("https://shop.test/wp-json/wc/v3/orders?"))
        self.assertNotIn("cs_secret", first.full_url)
        self.assertTrue(first.get_header("Authorization").startswith("Basic "))
        self.assertIn("page=2", op.call_args_list[1].args[0].full_url)

    def test_query_string_login_is_available_for_hosts_that_strip_the_header(self):
        client = WooClient("https://shop.test", "ck_key", "cs_secret", query_auth=True)
        with mock.patch("woosync.client.request.urlopen", return_value=FakeResponse(b"[]")) as op:
            client.get_page("orders")
        req = op.call_args.args[0]
        self.assertIn("consumer_key=ck_key", req.full_url)
        self.assertIsNone(req.get_header("Authorization"))

    def test_errors_are_friendly_and_never_contain_the_keys(self):
        client = WooClient("https://shop.test", "ck_key", "cs_secret")
        for code, word in ((401, "refused"), (404, "not found"), (500, "error")):
            failure = error.HTTPError("https://shop.test/x", code, "x", {}, None)
            with mock.patch("woosync.client.request.urlopen", side_effect=failure):
                with self.assertRaises(WooError) as ctx:
                    client.get_page("orders")
            text = str(ctx.exception)
            self.assertIn(word, text.lower())
            self.assertNotIn("cs_secret", text)
            self.assertNotIn("ck_key", text)

    def test_network_problems_are_friendly(self):
        client = WooClient("https://shop.test", "ck_key", "cs_secret")
        for failure, word in ((error.URLError("boom"), "reached"), (socket.timeout(), "too long")):
            with mock.patch("woosync.client.request.urlopen", side_effect=failure):
                with self.assertRaises(WooError) as ctx:
                    client.get_page("orders")
            self.assertIn(word, str(ctx.exception))

    def test_a_non_json_answer_is_explained(self):
        client = WooClient("https://shop.test", "ck_key", "cs_secret")
        with mock.patch("woosync.client.request.urlopen", return_value=FakeResponse(b"<html>hi</html>")):
            with self.assertRaises(WooError) as ctx:
                client.get_page("orders")
        self.assertIn("Permalinks", str(ctx.exception))


@override_settings(WOOSYNC={"URL": "https://shop.test", "KEY": "ck_key", "SECRET": "cs_secret"})
class ViewTests(Base):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user("w_boss", password="pw12345!", role="admin")
        self.cashier = User.objects.create_user("w_cash", password="pw12345!", role="cashier")
        apply_order(order())
        self.order = WebOrder.objects.get()

    def login(self, name):
        self.client.logout()
        self.client.login(username=name, password="pw12345!")

    def test_a_cashier_can_work_the_orders(self):
        self.login("w_cash")
        with mock.patch("woosync.views.WooClient", side_effect=AssertionError("orders refresh must not sync")):
            response = self.client.get(reverse("woosync:orders"))
        self.assertContains(response, "Asha")
        self.assertContains(response, "Website Orders")
        self.assertNotContains(response, "Website Integration")
        self.assertContains(self.client.get(reverse("woosync:orders"), {"q": "9876"}), "Asha")
        self.assertNotContains(self.client.get(reverse("woosync:orders"), {"q": "zzz"}), "Asha")
        self.assertEqual(self.client.get(reverse("woosync:order_detail", args=[self.order.pk])).status_code, 200)
        self.client.post(reverse("woosync:order_handled", args=[self.order.pk]))
        self.order.refresh_from_db()
        self.assertTrue(self.order.handled)
        self.assertEqual(self.order.handled_by, self.cashier)
        self.assertNotContains(self.client.get(reverse("woosync:orders")), "Asha")             # no longer "to handle"
        self.assertContains(self.client.get(reverse("woosync:orders"), {"show": "active"}), "Asha")

    def test_a_cashier_cannot_open_the_sync_pages(self):
        self.login("w_cash")
        for name in ("home", "products", "preview", "log"):
            self.assertEqual(self.client.get(reverse(f"woosync:{name}")).status_code, 403, name)
        for name in ("test", "toggle", "settings", "match", "sync"):
            self.assertEqual(self.client.post(reverse(f"woosync:{name}")).status_code, 403, name)
        self.assertEqual(self.client.post(reverse("woosync:order_recheck", args=[self.order.pk])).status_code, 403)
        self.assertFalse(SyncState.load().is_enabled)

    def test_orders_need_a_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("woosync:orders")).status_code, 302)

    def test_an_admin_can_open_every_page(self):
        self.login("w_boss")
        for name in ("home", "products", "preview", "log", "orders"):
            self.assertEqual(self.client.get(reverse(f"woosync:{name}")).status_code, 200, name)

    def test_admin_navigation_groups_website_sync_orders_and_history(self):
        self.login("w_boss")
        response = self.client.get(reverse("woosync:home"))
        self.assertContains(response, "Website Integration")
        self.assertContains(response, f'href="{reverse("woosync:home")}"')
        self.assertContains(response, f'href="{reverse("woosync:orders")}"')
        self.assertContains(response, f'href="{reverse("woosync:log")}"')
        self.assertContains(response, "Sync History")

    def test_product_conflict_can_be_resolved_using_the_pos_value(self):
        self.login("w_boss")
        conflict = ProductSyncConflict.objects.create(
            product=self.product,
            field_name="name",
            pos_value="POS Resin",
            woo_value="Website Resin",
        )
        self.assertContains(self.client.get(reverse("woosync:products")), "Conflicting product edits")

        response = self.client.post(
            reverse("woosync:resolve_conflict", args=[conflict.pk]),
            {"resolution": "pos"},
        )

        conflict.refresh_from_db()
        job = ProductSyncJob.objects.get(product=self.product)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(conflict.resolution, "pos")
        self.assertEqual(job.payload["force_fields"], ["name"])
        self.assertNotContains(self.client.get(reverse("woosync:products")), "Conflicting product edits")

    def test_deleting_a_linked_pos_product_archives_it_and_preserves_its_mapping(self):
        product = Product.objects.create(name="Linked product")
        link = WooLink.objects.create(product=product, woo_product_id=877, woo_type="simple")
        self.login("w_boss")

        response = self.client.post(reverse("inventory:product_delete", args=[product.pk]))

        product.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(product.is_active)
        self.assertEqual(WooLink.objects.get(pk=link.pk).woo_product_id, 877)

    def test_sync_now_view_imports_orders_from_the_website(self):
        self.login("w_boss")
        state = SyncState.load()
        state.is_enabled = True
        state.save()
        client = FakeClient(orders=[order(woo_id=301)])

        with mock.patch("woosync.sync.WooClient", return_value=client):
            response = self.client.post(reverse("woosync:sync"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(WebOrder.objects.filter(woo_id=301).exists())
        self.assertEqual(SyncRun.objects.get().trigger, "manual")

    def test_website_sync_models_are_registered_read_only_in_django_admin(self):
        for model in (SyncState, SyncRun, WebOrder, WebOrderItem, WooLink):
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                self.assertFalse(model_admin.has_add_permission(None))
                self.assertFalse(model_admin.has_delete_permission(None))
                self.assertEqual(
                    set(model_admin.get_readonly_fields(None)),
                    {field.name for field in model._meta.fields},
                )

    def test_django_admin_can_open_website_sync_settings_and_history(self):
        superuser = User.objects.create_superuser("woo_admin", password="admin-password")
        self.client.force_login(superuser)

        self.assertContains(self.client.get(reverse("admin:index")), "Website Sync")
        for model in ("syncstate", "syncrun", "weborder", "weborderitem", "woolink"):
            with self.subTest(model=model):
                response = self.client.get(reverse(f"admin:woosync_{model}_changelist"))
                self.assertEqual(response.status_code, 200)

    def test_sync_dashboard_shows_order_counts_cutoff_and_error(self):
        self.login("w_boss")
        cutoff = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
        state = SyncState.load()
        state.is_enabled = True
        state.orders_since = cutoff
        state.last_run_at = timezone.now()
        state.last_ok = False
        state.last_message = "Order #101 could not be imported."
        state.save()
        SyncRun.objects.create(trigger="manual", ok=False, orders_new=2, orders_failed=1)

        response = self.client.get(reverse("woosync:home"))
        self.assertContains(response, "Failed")
        self.assertEqual(response.context["imported_orders"], 1)
        self.assertEqual(response.context["latest_run"].orders_new, 2)
        self.assertEqual(response.context["latest_run"].orders_failed, 1)
        self.assertContains(response, "Imported on last run: <strong>2</strong>")
        self.assertContains(response, "Failed on last run: <strong>1</strong>")
        self.assertContains(response, "First sync cutoff:")
        self.assertContains(response, "Order #101 could not be imported.")

    def test_sync_dashboard_reports_running_state(self):
        self.login("w_boss")
        state = SyncState.load()
        state.running_since = timezone.now()
        state.save()

        self.assertContains(self.client.get(reverse("woosync:home")), "Running")

    def test_switching_on_and_off_keeps_the_start_date(self):
        self.login("w_boss")
        self.client.post(reverse("woosync:toggle"))
        state = SyncState.load()
        self.assertTrue(state.is_enabled)
        since = state.orders_since
        self.assertIsNotNone(since)
        self.client.post(reverse("woosync:toggle"))
        self.assertFalse(SyncState.load().is_enabled)
        self.client.post(reverse("woosync:toggle"))
        self.assertEqual(SyncState.load().orders_since, since)

    def test_it_cannot_be_switched_on_without_the_connection_settings(self):
        self.login("w_boss")
        with override_settings(WOOSYNC={}):
            self.client.post(reverse("woosync:toggle"))
        self.assertFalse(SyncState.load().is_enabled)

    def test_marking_products_as_sold_online(self):
        self.login("w_boss")
        url = reverse("woosync:products")
        self.client.post(url, {"action": "enable", "sel": [self.product.pk]})
        self.assertTrue(WooLink.objects.get(product=self.product).sell_online)
        self.client.post(url, {"action": "disable", "sel": [self.product.pk]})
        self.assertFalse(WooLink.objects.get(product=self.product).sell_online)

    def test_products_without_stock_tracking_can_be_marked_for_online_sale(self):
        self.login("w_boss")
        self.product.track_stock = False
        self.product.save()
        self.client.post(reverse("woosync:products"), {"action": "enable", "sel": [self.product.pk]})
        self.assertTrue(WooLink.objects.filter(product=self.product, sell_online=True).exists())
        job = ProductSyncJob.objects.get(product=self.product)
        self.assertEqual(job.event_type, "created")

    def test_the_holdback_is_checked(self):
        self.login("w_boss")
        self.client.post(reverse("woosync:settings"), {"holdback": "2"})
        self.assertEqual(SyncState.load().holdback_units, 2)
        self.client.post(reverse("woosync:settings"), {"holdback": "abc"})
        self.client.post(reverse("woosync:settings"), {"holdback": "5000"})
        self.assertEqual(SyncState.load().holdback_units, 2)

    def test_recheck_takes_stock_for_a_fixed_sku(self):
        apply_order(order(woo_id=300, lines=[line(sku="LATER", product_id=999)]))
        later = WebOrder.objects.get(woo_id=300)
        other = Product.objects.create(name="Later", code="LATER", sale_price=Decimal("10"))
        StockMovement.objects.create(product=other, quantity=5, movement_type="opening")
        self.login("w_boss")
        self.client.post(reverse("woosync:order_recheck", args=[later.pk]))
        self.assertEqual(self.stock(other), Decimal("3"))

    def test_the_pos_screen_shows_the_number_of_new_website_orders(self):
        self.login("w_cash")
        self.assertEqual(self.client.get(reverse("pos:screen")).context["web_new"], 1)
        self.client.post(reverse("woosync:order_handled", args=[self.order.pk]))
        self.assertEqual(self.client.get(reverse("pos:screen")).context["web_new"], 0)


class ProductWebhookTests(TestCase):
    def signed_request(self, body, delivery_id="delivery-1", signature_override=None):
        secret = "test-webhook-secret"
        signature = base64.b64encode(
            hmac.new(secret.encode(), body, hashlib.sha256).digest()
        ).decode()
        return self.client.post(
            reverse("woosync:product_webhook"),
            data=body,
            content_type="application/json",
            HTTP_X_WC_WEBHOOK_SIGNATURE=signature_override or signature,
            HTTP_X_WC_WEBHOOK_TOPIC="product.updated",
            HTTP_X_WC_WEBHOOK_DELIVERY_ID=delivery_id,
        )

    @override_settings(WOOSYNC={"WEBHOOK_SECRET": "test-webhook-secret"})
    def test_signed_webhook_is_queued_once_per_delivery(self):
        body = json.dumps({"id": 733}).encode()

        first = self.signed_request(body)
        duplicate = self.signed_request(body)

        self.assertEqual(first.status_code, 202)
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(ProductSyncJob.objects.filter(woo_product_id=733).count(), 1)

    @override_settings(WOOSYNC={"WEBHOOK_SECRET": "test-webhook-secret"})
    def test_invalid_webhook_signature_is_rejected(self):
        response = self.signed_request(b'{"id": 733}', signature_override="invalid")

        self.assertEqual(response.status_code, 401)
        self.assertFalse(ProductSyncJob.objects.exists())