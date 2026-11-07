from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from inventory.models import Product, StockMovement
from inventory.stock import qty_fmt, with_stock

from .models import Purchase, PurchaseCounter, PurchaseItem, PurchasePayment

TWO = Decimal("0.01")
THREE = Decimal("0.001")
MAX_LINES = 200
MAX_MONEY = Decimal("100000000")
MAX_QTY = Decimal("1000000")
COUNT_UNITS = {"pcs", "set", "box", "pack"}          # must be bought in whole numbers

# After a purchase, a product's cost price becomes the latest cost (after discount, before GST).
# The dashboard's Gross Profit uses this. Set to False to manage cost prices by hand.
UPDATE_COST_ON_PURCHASE = True


class PurchaseError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------- Input checks ----------
def _decimal(value, label):
    if value is None or isinstance(value, bool):
        raise PurchaseError(f"{label} is required.")
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        raise PurchaseError(f"{label} is not a valid number.")
    if not d.is_finite():
        raise PurchaseError(f"{label} is not a valid number.")
    return d


def money(value, label, upper=MAX_MONEY, blank_zero=False):
    if blank_zero and (value is None or str(value).strip() == ""):
        return Decimal("0.00")
    d = _decimal(value, label)
    if d < 0 or d > upper:
        raise PurchaseError(f"{label} is out of range.")
    if d != d.quantize(TWO):
        raise PurchaseError(f"{label} can have at most 2 decimal places.")
    return d.quantize(TWO)


def quantity(value, unit, name):
    d = _decimal(value, f"Quantity for {name}")
    if d <= 0 or d > MAX_QTY:
        raise PurchaseError(f"Quantity for {name} must be above zero.")
    if d != d.quantize(THREE):
        raise PurchaseError(f"Quantity for {name} can have at most 3 decimal places.")
    if unit in COUNT_UNITS and d != d.to_integral_value():
        raise PurchaseError(f"{name} must be bought in whole numbers.")
    return d.quantize(THREE)


def parse_date(text, label="Date"):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise PurchaseError(f"{label} is not a valid date.")


def line_totals(qty, cost, discount, gst_rate, name):
    gross = (qty * cost).quantize(TWO, ROUND_HALF_UP)
    if discount > gross:
        raise PurchaseError(f"Discount for {name} is more than the line amount.")
    taxable = gross - discount
    gst_amount = (taxable * gst_rate / 100).quantize(TWO, ROUND_HALF_UP)
    return gross, taxable, gst_amount, taxable + gst_amount


def parse_lines(post):
    """Reads the purchase form: product, quantity_N, cost_price_N, discount_N, gst_rate_N."""
    picked = []
    for index, pid in enumerate(post.getlist("product")):
        pid = (pid or "").strip()
        if not pid:
            continue
        if not pid.isdigit():
            raise PurchaseError("A product on the purchase is invalid.")
        picked.append((index, int(pid)))

    if not picked:
        raise PurchaseError("Add at least one product to the purchase.")
    if len(picked) > MAX_LINES:
        raise PurchaseError(f"A purchase can have at most {MAX_LINES} lines.")
    ids = [pid for _, pid in picked]
    if len(set(ids)) != len(ids):
        raise PurchaseError("The same product is on the purchase twice. Combine it into one line.")

    products = {p.pk: p for p in Product.objects.filter(pk__in=ids, is_active=True)}
    lines = []
    for index, pid in picked:
        p = products.get(pid)
        if p is None:
            raise PurchaseError(f"Product #{pid} is no longer available.")
        qty = quantity(post.get(f"quantity_{index}"), p.unit, p.name)
        cost = money(post.get(f"cost_price_{index}"), f"Cost price for {p.name}")
        disc = money(post.get(f"discount_{index}"), f"Discount for {p.name}", blank_zero=True)
        gst = money(post.get(f"gst_rate_{index}"), f"GST % for {p.name}", upper=Decimal("100"), blank_zero=True)
        gross, taxable, gst_amount, total = line_totals(qty, cost, disc, gst, p.name)
        lines.append({
            "product": p, "qty": qty, "cost": cost, "discount": disc, "gst_rate": gst,
            "gross": gross, "taxable": taxable, "gst_amount": gst_amount, "total": total,
        })
    return lines


# ---------- Create ----------
def create_purchase(user, header, lines, pay_amount, pay_method, pay_reference="", token=None):
    """Returns (purchase, already_done). Saves the purchase, its lines, the stock-in rows and any payment together."""
    token = (token or "")[:40] or None
    if token:
        existing = Purchase.objects.filter(client_token=token).first()
        if existing:
            return existing, True

    if pay_method not in PurchasePayment.Method.values:
        raise PurchaseError("Unknown payment method.")
    notes = (header.get("notes") or "").strip()
    if len(notes) > 255:
        raise PurchaseError("Notes can be at most 255 characters.")

    subtotal = sum((l["gross"] for l in lines), Decimal("0"))
    discount_total = sum((l["discount"] for l in lines), Decimal("0"))
    taxable_total = sum((l["taxable"] for l in lines), Decimal("0"))
    gst_total = sum((l["gst_amount"] for l in lines), Decimal("0"))
    grand = taxable_total + gst_total            # equals the sum of the line totals, to the paisa

    pay_amount = Decimal(pay_amount or 0).quantize(TWO)
    if pay_amount < 0 or pay_amount > grand:
        raise PurchaseError("The payment cannot be negative or more than the purchase total.")

    ordered = sorted(lines, key=lambda l: l["product"].pk)

    try:
        with transaction.atomic():
            number = PurchaseCounter.next_purchase_no()
            purchase = Purchase.objects.create(
                purchase_no=number, client_token=token, supplier=header["supplier"],
                supplier_invoice_no=(header.get("supplier_invoice_no") or "").strip(),
                purchase_date=header["purchase_date"],
                subtotal=subtotal, discount_total=discount_total, taxable_amount=taxable_total,
                cgst=Decimal("0.00"), sgst=Decimal("0.00"), igst=gst_total,
                grand_total=grand, amount_paid=pay_amount,
                status=Purchase.Status.COMPLETED, notes=notes, created_by=user,
            )

            PurchaseItem.objects.bulk_create([
                PurchaseItem(
                    purchase=purchase, product=l["product"], product_name=l["product"].name,
                    product_code=l["product"].code or "", hsn_code=l["product"].hsn_code or "",
                    unit=l["product"].unit, quantity=l["qty"], cost_price=l["cost"],
                    discount=l["discount"], gst_rate=l["gst_rate"], taxable_amount=l["taxable"],
                    gst_amount=l["gst_amount"], total_amount=l["total"],
                )
                for l in ordered
            ])

            StockMovement.objects.bulk_create([
                StockMovement(
                    product=l["product"], quantity=l["qty"], movement_type=StockMovement.Type.PURCHASE,
                    reference=number, note=f"Purchase {number}", created_by=user,
                )
                for l in ordered if l["product"].track_stock
            ])

            if pay_amount > 0:
                PurchasePayment.objects.create(
                    purchase=purchase, method=pay_method, amount=pay_amount,
                    payment_date=header["purchase_date"], reference=(pay_reference or "").strip()[:80],
                    recorded_by=user,
                )

            if UPDATE_COST_ON_PURCHASE:
                now = timezone.now()
                for l in ordered:
                    if l["taxable"] > 0:
                        new_cost = (l["taxable"] / l["qty"]).quantize(TWO, ROUND_HALF_UP)
                        if new_cost < Decimal("99999999.99"):
                            Product.objects.filter(pk=l["product"].pk).update(cost_price=new_cost, updated_at=now)

            return purchase, False

    except IntegrityError:
        # Two identical submits raced each other: return the one that won
        if token:
            existing = Purchase.objects.filter(client_token=token).first()
            if existing:
                return existing, True
        raise


# ---------- Pay a supplier later ----------
def add_payment(user, purchase_id, data):
    amount = money(data.get("amount"), "Payment amount")
    if amount <= 0:
        raise PurchaseError("Payment amount must be above zero.")
    method = data.get("method") or PurchasePayment.Method.CASH
    if method not in PurchasePayment.Method.values:
        raise PurchaseError("Unknown payment method.")
    pay_date = parse_date(data.get("payment_date"), "Payment date") or timezone.localdate()

    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().filter(pk=purchase_id).first()
        if purchase is None:
            raise PurchaseError("Purchase not found.", status=404)
        if purchase.status != Purchase.Status.COMPLETED:
            raise PurchaseError("Payments can only be added to an active purchase.")
        if amount > purchase.outstanding_amount:
            raise PurchaseError("Payment amount cannot be more than the outstanding balance.")

        PurchasePayment.objects.create(
            purchase=purchase, method=method, amount=amount, payment_date=pay_date,
            reference=(data.get("reference") or "").strip()[:80],
            notes=(data.get("notes") or "").strip()[:255], recorded_by=user,
        )
        purchase.amount_paid = purchase.amount_paid + amount
        purchase.save(update_fields=["amount_paid", "outstanding_amount", "updated_at"])
    return purchase


# ---------- Cancel ----------
def cancel_purchase(user, purchase_id, reason):
    """Reverses the stock this purchase added. Returns (purchase, warnings)."""
    reason = (reason or "").strip()[:255] or "Cancelled by admin"
    warnings = []

    with transaction.atomic():
        purchase = Purchase.objects.select_for_update().filter(pk=purchase_id).first()
        if purchase is None:
            raise PurchaseError("Purchase not found.", status=404)
        if purchase.status == Purchase.Status.CANCELLED:
            raise PurchaseError("This purchase is already cancelled.")

        # Reverse exactly the stock rows this purchase created
        received = list(StockMovement.objects.filter(
            reference=purchase.purchase_no, movement_type=StockMovement.Type.PURCHASE,
        ))
        StockMovement.objects.bulk_create([
            StockMovement(
                product_id=m.product_id, quantity=-m.quantity, movement_type=StockMovement.Type.PURCHASE_RETURN,
                reference=purchase.purchase_no, note="Purchase cancelled", created_by=user,
            )
            for m in received
        ])

        purchase.status = Purchase.Status.CANCELLED
        purchase.cancel_reason = reason
        purchase.cancelled_by = user
        purchase.save(update_fields=["status", "cancel_reason", "cancelled_by", "updated_at"])

        for p in with_stock(Product.objects.filter(pk__in={m.product_id for m in received})):
            if p.stock < 0:
                warnings.append(f"{p.name}: stock is now {qty_fmt(p.stock)}. Some of it was already sold, so please check the count.")
        if purchase.amount_paid > 0:
            warnings.append(f"\u20b9{purchase.amount_paid:,.2f} was already paid to the supplier. Recover it outside the system.")

    return purchase, warnings