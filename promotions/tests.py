import json
from datetime import datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from inventory.models import Brand, Category, Product, StockMovement
from pos.models import Sale
from pos.services import compute_bill
from storeconfig.models import StoreSettings

from .engine import best_discount, rules_for
from .models import Promotion, PromotionItem
from .services import PromotionError, parse_items, save_promotion

TUESDAY_NOON = timezone.make_aware(datetime(2026, 11, 10, 12, 0))      # 10 Nov 2026 is a Tuesday


def rule(kind="percent", value=1000, min_milli=0, name="Promo"):
    return {"name": name, "kind": kind, "value": value, "min": min_milli}


class EngineTests(TestCase):
    def test_percent_off_the_line(self):
        self.assertEqual(best_discount([rule(value=1000)], 25000, 2000), (5000, "Promo"))        # 10% of Rs 500

    def test_amount_is_off_each_unit(self):
        self.assertEqual(best_discount([rule("amount", 2000)], 25000, 3000)[0], 6000)            # Rs 20 x 3

    def test_quantity_condition(self):
        r = [rule(value=1000, min_milli=5000)]
        self.assertEqual(best_discount(r, 25000, 4000)[0], 0)
        self.assertEqual(best_discount(r, 25000, 5000)[0], 12500)                                 # 10% of Rs 1250

    def test_the_best_rule_wins_and_they_never_stack(self):
        rules = [rule(value=1000, name="Ten"), rule("amount", 3000, name="Thirty off"), rule(value=500, name="Five")]
        self.assertEqual(best_discount(rules, 25000, 2000), (6000, "Thirty off"))

    def test_a_tie_keeps_the_first(self):
        rules = [rule(value=1000, name="First"), rule("amount", 2500, name="Second")]
        self.assertEqual(best_discount(rules, 25000, 1000)[1], "First")

    def test_the_discount_never_exceeds_the_line(self):
        self.assertEqual(best_discount([rule("amount", 99999)], 25000, 2000)[0], 50000)

    def test_tiers_work_through_the_quantity_condition(self):
        tiers = [rule(value=1000, min_milli=5000, name="Five"), rule(value=1500, min_milli=10000, name="Ten")]
        self.assertEqual(best_discount(tiers, 10000, 6000)[1], "Five")
        self.assertEqual(best_discount(tiers, 10000, 10000)[1], "Ten")


class ComputeBillTests(TestCase):
    def test_promotion_comes_off_first_then_the_cashier_discount(self):
        line = {"price": 25000, "qty_milli": 2000, "gst_bp": 1800, "disc_kind": "percent", "disc_value": 1000, "promo": 5000}
        calc = compute_bill([line])
        self.assertEqual(calc["promo_total"], 5000)
        self.assertEqual(calc["discount_total"], 5000 + 4500)         # promo 50, then 10% of the remaining 450
        self.assertEqual(calc["total"], 40500)

    def test_a_bill_without_promotions_is_unchanged(self):
        calc = compute_bill([{"price": 25000, "qty_milli": 2000, "gst_bp": 1800, "disc_kind": None, "disc_value": 0}])
        self.assertEqual((calc["total"], calc["tax_total"], calc["promo_total"]), (50000, 7627, 0))


class RulesForTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name="Resin")
        self.brand = Brand.objects.create(name="Studio")
        self.product = Product.objects.create(
            name="Resin", sale_price=Decimal("250.00"), category=self.category, brand=self.brand,
        )

    def promo(self, **kw):
        p = Promotion.objects.create(name=kw.pop("name", "P"), **kw)
        PromotionItem.objects.create(promotion=p, product=self.product, kind="percent", value=Decimal("10"))
        return p

    def count(self, now=TUESDAY_NOON):
        return len(rules_for([self.product.pk], now).get(self.product.pk, []))

    def test_an_active_promotion_applies(self):
        self.promo()
        self.assertEqual(self.count(), 1)

    def test_off_scheduled_and_expired_do_not_apply(self):
        self.promo(is_active=False)
        self.promo(starts_at=TUESDAY_NOON + timedelta(hours=1))
        self.promo(ends_at=TUESDAY_NOON - timedelta(hours=1))
        self.assertEqual(self.count(), 0)

    def test_the_time_window_is_respected(self):
        self.promo(starts_at=TUESDAY_NOON - timedelta(hours=1), ends_at=TUESDAY_NOON + timedelta(hours=1))
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.count(TUESDAY_NOON + timedelta(hours=2)), 0)

    def test_weekdays(self):
        self.promo(weekdays="1")                                     # Tuesday only
        self.promo(weekdays="0,2")                                   # Monday and Wednesday
        self.assertEqual(self.count(), 1)

    def test_blank_weekdays_means_every_day(self):
        self.promo(weekdays="")
        self.assertEqual(self.count(TUESDAY_NOON + timedelta(days=2)), 1)

    def test_category_promotion_applies_to_matching_products(self):
        promotion = Promotion.objects.create(name="Category sale")
        PromotionItem.objects.create(promotion=promotion, category=self.category, kind="percent", value=Decimal("15"))
        self.assertEqual(rules_for([self.product.pk])[self.product.pk][0]["value"], 1500)

    def test_brand_promotion_applies_to_matching_products(self):
        promotion = Promotion.objects.create(name="Brand sale")
        PromotionItem.objects.create(promotion=promotion, brand=self.brand, kind="percent", value=Decimal("20"))
        self.assertEqual(rules_for([self.product.pk])[self.product.pk][0]["value"], 2000)


class ParseItemsTests(TestCase):
    def setUp(self):
        self.pcs = Product.objects.create(name="Pen", sale_price=Decimal("50.00"), unit="pcs")
        self.kg = Product.objects.create(name="Glue", sale_price=Decimal("80.00"), unit="kg")
        self.off = Product.objects.create(name="Old", sale_price=Decimal("10.00"), is_active=False)
        self.category = Category.objects.create(name="Art supplies")
        self.brand = Brand.objects.create(name="Studio")

    def post(self, *rows):
        from django.http import QueryDict
        q = QueryDict(mutable=True)
        q.setlist("target_type", [r.get("target_type", "product") for r in rows])
        q.setlist("target_id", [
            str(r.get(r.get("target_type", "product")).pk)
            if r.get(r.get("target_type", "product")) else ""
            for r in rows
        ])
        for i, r in enumerate(rows):
            q[f"kind_{i}"] = r.get("kind", "percent")
            q[f"value_{i}"] = r.get("value", "10")
            if r.get("min"):
                q[f"min_qty_{i}"] = r["min"]
        return q

    def test_a_good_row_parses(self):
        rows = parse_items(self.post({"product": self.pcs, "min": "5"}))
        self.assertEqual(rows[0]["min_qty"], Decimal("5.000"))
        self.assertEqual(rows[0]["value"], Decimal("10.00"))

    def test_bad_rows_are_rejected(self):
        bad = [
            [], [{"product": None}],
            [{"product": self.pcs, "value": "0"}], [{"product": self.pcs, "value": "101"}],
            [{"product": self.pcs, "value": "abc"}], [{"product": self.pcs, "value": "10.555"}],
            [{"product": self.pcs, "kind": "amount", "value": "60"}],          # more than the price
            [{"product": self.pcs, "kind": "weird"}],
            [{"product": self.pcs, "min": "2.5"}],                              # pieces need whole numbers
            [{"product": self.pcs, "min": "0"}],
            [{"product": self.off}],                                            # inactive product
            [{"product": self.pcs}, {"product": self.pcs}],                     # same product, same condition
        ]
        for rows in bad:
            with self.assertRaises(PromotionError, msg=str(rows)):
                parse_items(self.post(*rows))

    def test_the_same_product_with_different_conditions_is_allowed(self):
        rows = parse_items(self.post({"product": self.pcs, "min": "5"}, {"product": self.pcs, "min": "10", "value": "15"}))
        self.assertEqual(len(rows), 2)

    def test_decimal_minimum_is_fine_for_weight_products(self):
        self.assertEqual(parse_items(self.post({"product": self.kg, "min": "0.5"}))[0]["min_qty"], Decimal("0.500"))

    def test_category_and_brand_targets_parse(self):
        rows = parse_items(self.post(
            {"target_type": "category", "category": self.category},
            {"target_type": "brand", "brand": self.brand, "value": "20"},
        ))
        self.assertEqual([row["target_type"] for row in rows], ["category", "brand"])
        self.assertEqual(rows[0]["category"], self.category)
        self.assertEqual(rows[1]["brand"], self.brand)

    def test_saving_replaces_the_old_items(self):
        promo = Promotion.objects.create(name="X")
        save_promotion(promo, parse_items(self.post({"product": self.pcs}, {"product": self.kg})))
        self.assertEqual(promo.items.count(), 2)
        save_promotion(promo, parse_items(self.post({"product": self.kg})))
        self.assertEqual(promo.items.count(), 1)


class PromotionPageTests(TestCase):
    def setUp(self):
        User.objects.create_user("pr_boss", password="pw12345!", role="admin")
        User.objects.create_user("pr_cash", password="pw12345!", role="cashier")
        self.product = Product.objects.create(name="Resin", sale_price=Decimal("250.00"))
        self.client.login(username="pr_boss", password="pw12345!")

    def data(self, **over):
        d = {"name": "Diwali", "is_active": "on", "all_days": "on", "starts_at": "", "ends_at": "",
             "product": [str(self.product.pk)], "kind_0": "percent", "value_0": "10"}
        d.update(over)
        return d

    def test_create_page_offers_all_target_types(self):
        response = self.client.get(reverse("promotions:create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Product")
        self.assertContains(response, "Category")
        self.assertContains(response, "Brand")

    def test_admin_creates_a_promotion(self):
        response = self.client.post(reverse("promotions:create"), self.data(min_qty_0="5"))
        self.assertEqual(response.status_code, 302)
        promo = Promotion.objects.get()
        self.assertEqual(promo.weekdays, "")
        self.assertEqual(promo.items.get().min_qty, Decimal("5.000"))

    def test_admin_creates_category_and_brand_targets(self):
        category = Category.objects.create(name="Craft")
        brand = Brand.objects.create(name="Studio")
        data = self.data(
            target_type=["category", "brand"],
            target_id=[str(category.pk), str(brand.pk)],
            kind_1="percent", value_1="20",
        )
        data.pop("product")
        response = self.client.post(reverse("promotions:create"), data)
        self.assertEqual(response.status_code, 302)
        promo = Promotion.objects.get()
        self.assertEqual(promo.items.count(), 2)
        self.assertEqual(promo.items.get(category=category).value, Decimal("10.00"))
        self.assertEqual(promo.items.get(brand=brand).value, Decimal("20.00"))

    def test_chosen_days_are_saved(self):
        data = self.data(days=["0", "4"])
        data.pop("all_days")
        self.client.post(reverse("promotions:create"), data)
        self.assertEqual(Promotion.objects.get().weekdays, "0,4")

    def test_no_days_and_no_all_days_is_an_error(self):
        data = self.data()
        data.pop("all_days")
        response = self.client.post(reverse("promotions:create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Promotion.objects.count(), 0)

    def test_end_before_start_is_an_error(self):
        response = self.client.post(reverse("promotions:create"),
                                    self.data(starts_at="2026-11-10T10:00", ends_at="2026-11-10T09:00"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Promotion.objects.count(), 0)

    def test_a_failed_save_gives_the_typed_rows_back(self):
        response = self.client.post(reverse("promotions:create"), self.data(value_0="500"))
        self.assertEqual(Promotion.objects.count(), 0)
        self.assertEqual(response.context["rows"][0]["product"], str(self.product.pk))
        self.assertEqual(response.context["rows"][0]["value"], "500")

    def test_editing_replaces_the_items(self):
        self.client.post(reverse("promotions:create"), self.data())
        promo = Promotion.objects.get()
        self.client.post(reverse("promotions:edit", args=[promo.pk]), self.data(name="Renamed", value_0="20"))
        promo.refresh_from_db()
        self.assertEqual(promo.name, "Renamed")
        self.assertEqual(promo.items.get().value, Decimal("20.00"))

    def test_toggle_and_delete(self):
        self.client.post(reverse("promotions:create"), self.data())
        promo = Promotion.objects.get()
        self.client.post(reverse("promotions:toggle", args=[promo.pk]))
        promo.refresh_from_db()
        self.assertFalse(promo.is_active)
        self.client.post(reverse("promotions:delete", args=[promo.pk]))
        self.assertEqual(Promotion.objects.count(), 0)

    def test_cashiers_are_locked_out_of_every_page(self):
        promo = Promotion.objects.create(name="P")
        self.client.logout()
        self.client.login(username="pr_cash", password="pw12345!")
        for name, args in (("list", []), ("create", []), ("edit", [promo.pk]), ("delete", [promo.pk])):
            self.assertEqual(self.client.get(reverse(f"promotions:{name}", args=args)).status_code, 403, name)
        self.assertEqual(self.client.post(reverse("promotions:create"), self.data()).status_code, 403)
        self.client.post(reverse("promotions:toggle", args=[promo.pk]))
        promo.refresh_from_db()
        self.assertTrue(promo.is_active)

    def test_the_list_page_loads(self):
        self.client.post(reverse("promotions:create"), self.data())
        self.assertContains(self.client.get(reverse("promotions:list")), "Diwali")


class PosPromotionTests(TestCase):
    def setUp(self):
        User.objects.create_user("pp_boss", password="pw12345!", role="admin")
        User.objects.create_user("pp_cash", password="pw12345!", role="cashier")
        self.category = Category.objects.create(name="Resin")
        self.brand = Brand.objects.create(name="Studio")
        self.product = Product.objects.create(
            name="Resin", sale_price=Decimal("250.00"), mrp=Decimal("300.00"), gst_rate=Decimal("18"), unit="pcs",
            category=self.category, brand=self.brand,
        )
        StockMovement.objects.create(product=self.product, quantity=100, movement_type="opening")
        self.promo = Promotion.objects.create(name="Ten off")
        self.item = PromotionItem.objects.create(promotion=self.promo, product=self.product, kind="percent", value=Decimal("10"))
        self.client.login(username="pp_cash", password="pw12345!")

    def sell(self, qty="2", **extra):
        item = {"product_id": self.product.pk, "quantity": qty}
        item.update(extra.pop("item", {}))
        body = {"items": [item], "payments": [{"method": "cash", "amount": "9999.00"}]}
        body.update(extra)
        return self.client.post(reverse("pos:checkout"), data=json.dumps(body), content_type="application/json")

    def test_the_promotion_applies_at_checkout(self):
        data = self.sell().json()
        self.assertEqual(data["total"], "450.00")
        line = Sale.objects.get().items.get()
        self.assertEqual(line.promo_discount, Decimal("50.00"))
        self.assertEqual(line.promo_name, "Ten off")
        self.assertEqual(line.discount, Decimal("50.00"))
        self.assertEqual(line.tax_amount, Decimal("68.64"))          # GST on the Rs 450 that was actually paid

    def test_the_browser_cannot_claim_or_invent_a_promotion(self):
        self.promo.is_active = False
        self.promo.save()
        response = self.sell(item={"promo": "500", "promo_discount": "500"})
        self.assertEqual(response.json()["total"], "500.00")

    def test_a_promotion_that_ended_does_not_apply(self):
        self.promo.ends_at = timezone.now() - timedelta(minutes=1)
        self.promo.save()
        self.assertEqual(self.sell().json()["total"], "500.00")

    def test_quantity_condition_at_checkout(self):
        self.item.min_qty = Decimal("5")
        self.item.save()
        self.assertEqual(self.sell(qty="4").json()["total"], "1000.00")
        self.assertEqual(self.sell(qty="5").json()["total"], "1125.00")

    def test_a_stale_total_is_refused_with_409(self):
        response = self.sell(expected_total=50000)                    # the screen did not know about the promotion
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Sale.objects.count(), 0)

    def test_the_cashier_limit_ignores_the_promotion(self):
        cfg = StoreSettings.load()
        cfg.cashier_max_discount_percent = Decimal("10")
        cfg.save()
        self.item.value = Decimal("20")
        self.item.save()
        self.assertEqual(self.sell().status_code, 200)                                                       # 20% promotion only
        ok = self.sell(item={"discount": {"type": "percent", "value": "5"}})                                 # 5% of what is left
        self.assertEqual(ok.status_code, 200)
        too_much = self.sell(item={"discount": {"type": "percent", "value": "15"}})
        self.assertEqual(too_much.status_code, 400)

    def test_returns_refund_what_was_actually_paid(self):
        sale = Sale.objects.get(invoice_no=self.sell().json()["invoice_no"])
        line = sale.items.get()
        self.client.logout()
        self.client.login(username="pp_boss", password="pw12345!")
        self.client.post(reverse("pos:sale_return", args=[sale.pk]),
                         {"refund_method": "cash", "client_token": "pr1", f"qty_{line.pk}": "1"})
        self.assertEqual(sale.returns.get().refund_total, Decimal("225.00"))

    def test_search_results_carry_the_running_promotions(self):
        results = self.client.get(reverse("pos:product_search"), {"q": "resin"}).json()["results"]
        self.assertEqual(results[0]["promos"][0]["name"], "Ten off")
        self.assertEqual(results[0]["promos"][0]["value"], 1000)

    def test_category_promotion_applies_at_checkout(self):
        PromotionItem.objects.create(
            promotion=Promotion.objects.create(name="Category deal"),
            category=self.category, kind="percent", value=Decimal("15"),
        )
        self.assertEqual(self.sell().json()["total"], "425.00")

    def test_brand_promotion_applies_at_checkout(self):
        PromotionItem.objects.create(
            promotion=Promotion.objects.create(name="Brand deal"),
            brand=self.brand, kind="percent", value=Decimal("20"),
        )
        self.assertEqual(self.sell().json()["total"], "400.00")

    def test_the_rules_endpoint_and_its_input_checking(self):
        url = reverse("pos:promotion_rules")
        data = self.client.get(url, {"ids": f"{self.product.pk},abc,,-5"}).json()
        self.assertEqual(data["rules"][str(self.product.pk)][0]["kind"], "percent")
        self.assertEqual(self.client.get(url, {"ids": ""}).json(), {"rules": {}})
        self.client.logout()
        self.assertEqual(self.client.get(url, {"ids": "1"}).status_code, 302)