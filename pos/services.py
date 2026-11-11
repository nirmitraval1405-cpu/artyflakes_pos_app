from decimal import Decimal, InvalidOperation
from promotions.engine import best_discount, rules_for
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from inventory.models import Product, StockMovement, Unit
from inventory.stock import current_stock, qty_fmt
from people.models import Contact
from storeconfig.models import StoreSettings

from .models import InvoiceCounter, Payment, ReturnCounter, Sale, SaleItem, SaleReturn, SaleReturnItem

MAX_LINES = 100
MAX_RUPEES = Decimal("10000000")        # sanity limit: Rs 1 crore per amount
class CheckoutError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------- Small helpers (all money is whole paise) ----------
def div_round(a, b):
    """a / b rounded half-up, for non-negative whole numbers."""
    return (2 * a + b) // (2 * b)


def rupees(paise):
    return (Decimal(paise) / 100).quantize(Decimal("0.01"))


def _money_text(paise):
    return f"{rupees(paise):,.2f}"


def _decimal(value, label):
    if isinstance(value, bool) or value is None or value == "":
        raise CheckoutError(f"{label} is required.")
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        raise CheckoutError(f"{label} is not a valid number.")
    if not d.is_finite():
        raise CheckoutError(f"{label} is not a valid number.")
    return d


def _hundredths(value, label, upper):
    """'12.50' -> 1250. Rejects negatives, more than 2 decimals, and values above `upper`."""
    d = _decimal(value, label)
    if d < 0 or d > upper:
        raise CheckoutError(f"{label} is out of range.")
    scaled = d * 100
    if scaled != scaled.to_integral_value():
        raise CheckoutError(f"{label} can have at most 2 decimal places.")
    return int(scaled)


def to_paise(value, label):
    return _hundredths(value, label, MAX_RUPEES)


def to_basis_points(value, label):
    return _hundredths(value, label, Decimal(100))


def parse_discount(spec, label):
    if not spec:
        return None, 0
    if not isinstance(spec, dict):
        raise CheckoutError(f"{label} is invalid.")
    kind = spec.get("type", "none")
    if kind == "none":
        return None, 0
    if kind == "percent":
        return "percent", to_basis_points(spec.get("value", 0), label)
    if kind == "amount":
        return "amount", to_paise(spec.get("value", 0), label)
    raise CheckoutError(f"{label} has an unknown type.")


def _apply_discount(base, kind, value):
    if kind == "percent":
        amount = div_round(base * value, 10000)
    elif kind == "amount":
        amount = value
    else:
        amount = 0
    return min(amount, base)


# ---------- The money maths (pure, no database) ----------
def compute_bill(lines, bill_kind=None, bill_value=0):
    """
    lines: list of {price, qty_milli, gst_bp, disc_kind, disc_value, promo}   (price in paise, GST in basis points)
    promo is the promotion discount on the line, in paise (optional, 0 when there is none).
    Prices include GST. Returns paise totals and per-line results.
    The browser (pos.js) runs the same steps; keep the two in sync.
    """
    rows = []
    for ln in lines:
        gross = div_round(ln["price"] * ln["qty_milli"], 1000)
        promo = min(ln.get("promo", 0), gross)                                  # the promotion comes off first
        line_disc = _apply_discount(gross - promo, ln["disc_kind"], ln["disc_value"])   # the cashier's discount comes after
        rows.append({"gross": gross, "promo": promo, "line_disc": line_disc,
                     "net": gross - promo - line_disc, "share": 0})

    base = sum(r["net"] for r in rows)
    bill_disc = _apply_discount(base, bill_kind, bill_value)

    # Share the bill discount across lines in proportion to what each line costs
    if bill_disc and base:
        for r in rows:
            r["share"] = bill_disc * r["net"] // base
        left = bill_disc - sum(r["share"] for r in rows)
        i = 0
        while left > 0:
            r = rows[i % len(rows)]
            if r["share"] < r["net"]:
                r["share"] += 1
                left -= 1
            i += 1

    result_lines = []
    for r, ln in zip(rows, lines):
        discount = r["promo"] + r["line_disc"] + r["share"]
        total = r["gross"] - discount
        gst_bp = ln["gst_bp"]
        tax = div_round(total * gst_bp, 10000 + gst_bp)    # GST already inside the price
        result_lines.append({"gross": r["gross"], "promo": r["promo"], "discount": discount, "tax": tax, "total": total})

    return {
        "lines": result_lines,
        "subtotal": sum(l["gross"] for l in result_lines),
        "promo_total": sum(l["promo"] for l in result_lines),
        "bill_discount": bill_disc,
        "discount_total": sum(l["discount"] for l in result_lines),
        "tax_total": sum(l["tax"] for l in result_lines),
        "total": sum(l["total"] for l in result_lines),
    }

# ---------- Checkout ----------
def _result(sale, warnings, duplicate=False):
    paid = sale.payments.aggregate(total=Sum("amount"))["total"] or Decimal("0")
    return {
        "ok": True,
        "duplicate": duplicate,
        "sale_id": sale.pk,
        "invoice_no": sale.invoice_no,
        "created": timezone.localtime(sale.created_at).strftime("%d-%m-%Y %H:%M"),
        "subtotal": f"{sale.subtotal:,.2f}",
        "discount": f"{sale.discount_total:,.2f}",
        "tax": f"{sale.tax_total:,.2f}",
        "total": f"{sale.total:.2f}",
        "paid": f"{sale.amount_paid:,.2f}",
        "due": f"{max(sale.total - paid, Decimal('0')):.2f}",
        "change": f"{sale.change_due:,.2f}",
        "customer_phone": "" if sale.customer.is_system else sale.customer.phone,
        "lines": [
            {
                "name": i.product_name,
                "qty": f"{i.quantity.normalize():f}",
                "price": f"{i.unit_price:,.2f}",
                "amount": f"{i.line_total:,.2f}",
            }
            for i in sale.items.all()
        ],
        "warnings": warnings,
    }


def create_sale(user, data):
    token = str(data.get("client_token") or "")[:40] or None
    if token:
        existing = Sale.objects.filter(client_token=token).first()
        if existing:
            return _result(existing, [], duplicate=True)

    # ---- 1. Read and check the request (no database changes yet) ----
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise CheckoutError("The bill is empty.")
    if len(raw_items) > MAX_LINES:
        raise CheckoutError(f"A bill can have at most {MAX_LINES} lines.")

    specs, seen = [], set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise CheckoutError("An item on the bill is invalid.")
        pid = raw.get("product_id")
        if not isinstance(pid, int) or isinstance(pid, bool):
            raise CheckoutError("An item on the bill is invalid.")
        if pid in seen:
            raise CheckoutError("The same product appears twice on the bill.")
        seen.add(pid)

        qty = _decimal(raw.get("quantity"), "Quantity")
        milli = qty * 1000
        if qty <= 0 or qty > 100000 or milli != milli.to_integral_value():
            raise CheckoutError("Quantity must be above zero with at most 3 decimals.")
        kind, value = parse_discount(raw.get("discount"), "Item discount")
        raw_price = raw.get("price")
        price = to_paise(raw_price, "Item price") if raw_price not in (None, "") else None
        if price == 0:
            raise CheckoutError("Item price must be above zero.")
        specs.append({
            "pid": pid, "qty_milli": int(milli), "disc_kind": kind, "disc_value": value, "price": price,
        })

    bill_kind, bill_value = parse_discount(data.get("bill_discount"), "Bill discount")

    pay_rows = data.get("payments")
    allow_credit = data.get("allow_credit") is True
    if not isinstance(pay_rows, list) or (not pay_rows and not allow_credit):
        raise CheckoutError("Add at least one payment.")
    payments = []
    for raw in pay_rows:
        if not isinstance(raw, dict):
            raise CheckoutError("A payment is invalid.")
        method = raw.get("method")
        if method not in Payment.Method.values:
            raise CheckoutError("Unknown payment method.")
        amount = to_paise(raw.get("amount"), "Payment amount")
        if amount <= 0:
            raise CheckoutError("Payment amounts must be above zero.")
        payments.append({"method": method, "amount": amount, "reference": str(raw.get("reference") or "")[:60]})

    customer_id = data.get("customer_id")
    expected_total = data.get("expected_total")

    # ---- 2. Save everything or nothing ----
    try:
        with transaction.atomic():
            # The shop's current details and discount limit, read from the Store Settings page
            cfg = StoreSettings.load()

            # Lock the products being sold (in a fixed order, so two checkouts cannot deadlock)
            products = {
                p.pk: p for p in Product.objects.select_for_update().filter(pk__in=seen).order_by("pk")
            }

            # The promotions running right now. The server decides this, never the browser.
            rules = rules_for(seen, timezone.now())
            whole_units = Unit.whole_quantity_codes()

            lines, promo_names = [], []
            for s in specs:
                p = products.get(s["pid"])
                if p is None:
                    raise CheckoutError("A product on the bill no longer exists.")
                if not p.is_active:
                    raise CheckoutError(f"{p.name} is inactive and cannot be sold.")
                if p.unit in whole_units and s["qty_milli"] % 1000:
                    raise CheckoutError(f"{p.name} must be sold in whole numbers.")
                price = s["price"] if s["price"] is not None else int((p.sale_price * 100).to_integral_value())
                promo, promo_name = best_discount(rules.get(p.pk, []), price, s["qty_milli"])
                promo_names.append(promo_name if promo else "")
                lines.append({
                    "price": price,
                    "qty_milli": s["qty_milli"],
                    "gst_bp": int((p.gst_rate * 100).to_integral_value()),
                    "disc_kind": s["disc_kind"],
                    "disc_value": s["disc_value"],
                    "promo": promo,
                })

            calc = compute_bill(lines, bill_kind, bill_value)
            total = calc["total"]

            if total <= 0:
                raise CheckoutError("Nothing to pay on this bill.")

            if expected_total is not None and expected_total != total:
                raise CheckoutError(
                    "The bill total changed (a price or a promotion may have changed). "
                    "The totals on your screen were refreshed. Please check them and try again.", status=409,
                )

            if not user.is_admin_role:
                # The cashier limit covers only the discount the cashier gives. Promotions are set by the owner.
                manual = calc["discount_total"] - calc["promo_total"]
                base = calc["subtotal"] - calc["promo_total"]
                max_pct = cfg.cashier_max_discount_percent
                max_bp = int(max_pct * 100)
                if manual * 10000 > max_bp * base:
                    raise CheckoutError(
                        f"Discount is above your limit of {max_pct.normalize():f}%. Ask an admin to bill this."
                    )

            tendered = sum(p["amount"] for p in payments)
            if tendered < total and not allow_credit:
                raise CheckoutError(f"Payment is short by \u20b9{_money_text(total - tendered)}.")
            cash = sum(p["amount"] for p in payments if p["method"] == Payment.Method.CASH)
            change = max(tendered - total, 0)
            if change > cash:
                raise CheckoutError("Only cash can be overpaid. UPI and card cannot be more than the bill.")

            if customer_id in (None, ""):
                customer = Contact.walk_in()
            else:
                customer = Contact.objects.filter(pk=customer_id, is_customer=True, is_active=True).first()
                if customer is None:
                    raise CheckoutError("That customer was not found.")
            if tendered < total and customer.is_system:
                raise CheckoutError("Credit sales require a named customer.")

            invoice_no = InvoiceCounter.next_invoice_no()
            sale = Sale.objects.create(
                invoice_no=invoice_no, customer=customer, cashier=user, client_token=token,
                subtotal=rupees(calc["subtotal"]), bill_discount=rupees(calc["bill_discount"]),
                discount_total=rupees(calc["discount_total"]), tax_total=rupees(calc["tax_total"]),
                total=rupees(total), amount_paid=rupees(tendered), change_due=rupees(change),
                note=str(data.get("note") or "")[:255],
                store_info=cfg.as_store_info(),     # the shop details this bill was made with
            )

            items, movements, warnings = [], [], []
            for s, line, result, promo_name in zip(specs, lines, calc["lines"], promo_names):
                p = products[s["pid"]]
                qty = (Decimal(s["qty_milli"]) / 1000).quantize(Decimal("0.001"))
                items.append(SaleItem(
                    sale=sale, product=p, product_name=p.name, product_code=p.code or "",
                    hsn_code=p.hsn_code, unit=p.unit, quantity=qty,
                    unit_price=rupees(line["price"]), mrp=p.mrp or p.sale_price, cost_price=p.cost_price,
                    gst_rate=p.gst_rate, discount=rupees(result["discount"]),
                    promo_discount=rupees(result["promo"]), promo_name=promo_name[:120],
                    tax_amount=rupees(result["tax"]), line_total=rupees(result["total"]),
                ))
                if p.track_stock:
                    before = current_stock(p)
                    movements.append(StockMovement(
                        product=p, quantity=-qty, movement_type=StockMovement.Type.SALE,
                        reference=invoice_no, note="Sale", created_by=user,
                    ))
                    if before - qty < 0:
                        warnings.append(f"{p.name}: stock is now {qty_fmt(before - qty)}. Please check the count.")
            SaleItem.objects.bulk_create(items)
            StockMovement.objects.bulk_create(movements)

            # Payments add up to the bill: cash is recorded after change is handed back
            pay_objs = [
                Payment(sale=sale, method=p["method"], amount=rupees(p["amount"]), reference=p["reference"])
                for p in payments if p["method"] != Payment.Method.CASH
            ]
            net_cash = cash - change
            if net_cash > 0:
                pay_objs.append(Payment(sale=sale, method=Payment.Method.CASH, amount=rupees(net_cash)))
            Payment.objects.bulk_create(pay_objs)

            return _result(sale, warnings)

    except IntegrityError:
        # Two identical submits raced each other: return the one that won
        if token:
            existing = Sale.objects.filter(client_token=token).first()
            if existing:
                return _result(existing, [], duplicate=True)
        raise

# =====================  Void and return  =====================
def _require_admin(user):
    if not user.is_admin_role:
        raise CheckoutError("Only an admin can do this.", status=403)


def void_sale(user, sale_id, reason):
    """Cancel a bill and put its stock back. The bill stays on record, marked VOIDED."""
    _require_admin(user)
    reason = (reason or "").strip()[:255]
    if not reason:
        raise CheckoutError("Give a reason for voiding this bill.")

    with transaction.atomic():
        sale = Sale.objects.select_for_update().filter(pk=sale_id).first()
        if sale is None:
            raise CheckoutError("Bill not found.", status=404)
        if sale.status == Sale.Status.VOIDED:
            raise CheckoutError("This bill is already voided.")
        if sale.returns.exists():
            raise CheckoutError("This bill already has returns, so it cannot be voided.")

        # Reverse exactly the stock rows this sale created
        sold = StockMovement.objects.filter(reference=sale.invoice_no, movement_type=StockMovement.Type.SALE)
        StockMovement.objects.bulk_create([
            StockMovement(
                product_id=m.product_id, quantity=-m.quantity, movement_type=StockMovement.Type.SALE_RETURN,
                reference=sale.invoice_no, note="Bill voided", created_by=user,
            )
            for m in sold
        ])

        sale.status = Sale.Status.VOIDED
        sale.voided_at = timezone.now()
        sale.voided_by = user
        sale.void_reason = reason
        sale.save(update_fields=["status", "voided_at", "voided_by", "void_reason", "updated_at"])
    return sale


def create_return(user, sale_id, data):
    """
    data: {"quantities": {sale_item_id: "1"}, "refund_method": "cash", "reason": "...", "client_token": "..."}
    Returns (SaleReturn, already_done).
    """
    _require_admin(user)

    token = str(data.get("client_token") or "")[:40] or None
    if token:
        existing = SaleReturn.objects.filter(client_token=token).first()
        if existing:
            return existing, True

    method = data.get("refund_method")
    if method not in Payment.Method.values:
        raise CheckoutError("Choose how the refund was paid.")
    reason = str(data.get("reason") or "").strip()[:255]

    wanted = {}
    for item_id, raw in (data.get("quantities") or {}).items():
        if str(raw).strip() == "":
            continue
        qty = _decimal(raw, "Quantity")
        milli = qty * 1000
        if qty < 0 or milli != milli.to_integral_value():
            raise CheckoutError("Quantities must be positive numbers with at most 3 decimals.")
        if qty > 0:
            wanted[int(item_id)] = int(milli)
    if not wanted:
        raise CheckoutError("Enter a quantity for at least one item.")

    try:
        with transaction.atomic():
            # Locking the bill makes two returns on the same bill take turns
            sale = Sale.objects.select_for_update().filter(pk=sale_id).first()
            if sale is None:
                raise CheckoutError("Bill not found.", status=404)
            if sale.status == Sale.Status.VOIDED:
                raise CheckoutError("This bill was voided, so nothing can be returned.")

            items = {i.pk: i for i in sale.items.all()}
            whole_units = Unit.whole_quantity_codes()
            if set(wanted) - set(items):
                raise CheckoutError("An item does not belong to this bill.")

            done = {
                row["sale_item_id"]: row
                for row in SaleReturnItem.objects.filter(sale_item__sale=sale)
                .values("sale_item_id")
                .annotate(q=Sum("quantity"), a=Sum("refund_amount"), t=Sum("tax_amount"))
            }

            lines, refund_total, tax_total = [], 0, 0
            for item_id in sorted(wanted):
                item = items[item_id]
                r_milli = wanted[item_id]
                q_milli = int(item.quantity * 1000)
                prev = done.get(item_id)
                back_milli = int(prev["q"] * 1000) if prev else 0
                left_milli = q_milli - back_milli

                if r_milli > left_milli:
                    raise CheckoutError(
                        f"{item.product_name}: only {qty_fmt(Decimal(left_milli) / 1000)} can still be returned."
                    )
                if item.unit in whole_units and r_milli % 1000:
                    raise CheckoutError(f"{item.product_name} must be returned in whole numbers.")

                line_paise = int(item.line_total * 100)
                paid_back = int(prev["a"] * 100) if prev else 0
                tax_paise = int(item.tax_amount * 100)
                tax_back = int(prev["t"] * 100) if prev else 0

                if r_milli == left_milli:
                    # The last part of a line gets whatever is left, so rounding never refunds extra
                    refund = line_paise - paid_back
                    tax = tax_paise - tax_back
                else:
                    refund = min(div_round(line_paise * r_milli, q_milli), line_paise - paid_back)
                    tax = min(div_round(tax_paise * r_milli, q_milli), tax_paise - tax_back)

                lines.append((item, r_milli, refund, tax))
                refund_total += refund
                tax_total += tax

            if refund_total <= 0:
                raise CheckoutError("There is nothing to refund for these items.")

            ret = SaleReturn.objects.create(
                return_no=ReturnCounter.next_return_no(), sale=sale, processed_by=user,
                refund_method=method, refund_total=rupees(refund_total), tax_total=rupees(tax_total),
                reason=reason, client_token=token,
            )
            SaleReturnItem.objects.bulk_create([
                SaleReturnItem(
                    sale_return=ret, sale_item=item, product_id=item.product_id, product_name=item.product_name,
                    quantity=(Decimal(r_milli) / 1000).quantize(Decimal("0.001")),
                    refund_amount=rupees(refund), tax_amount=rupees(tax),
                )
                for item, r_milli, refund, tax in lines
            ])

            # Put stock back only for products whose stock was reduced by this sale
            tracked = set(
                StockMovement.objects.filter(reference=sale.invoice_no, movement_type=StockMovement.Type.SALE)
                .values_list("product_id", flat=True)
            )
            StockMovement.objects.bulk_create([
                StockMovement(
                    product_id=item.product_id, quantity=(Decimal(r_milli) / 1000).quantize(Decimal("0.001")),
                    movement_type=StockMovement.Type.SALE_RETURN, reference=ret.return_no,
                    note=f"Return from {sale.invoice_no}", created_by=user,
                )
                for item, r_milli, refund, tax in lines
                if item.product_id in tracked
            ])
            return ret, False

    except IntegrityError:
        # Two identical submits raced each other: return the one that won
        if token:
            existing = SaleReturn.objects.filter(client_token=token).first()
            if existing:
                return existing, True
        raise