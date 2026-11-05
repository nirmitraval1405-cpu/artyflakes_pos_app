import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from inventory.models import Product, StockMovement
from inventory.stock import with_stock
from people.models import Contact

from .models import HeldSale, Sale, SaleItem

CENT = Decimal("0.01")
MILLI = Decimal("0.001")
ZERO = Decimal("0")
MAX_CART_ITEMS = 200


def _money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _json_body(request):
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JsonResponse({"error": "Request body must be valid JSON."}, status=400)
    if not isinstance(body, dict):
        return None, JsonResponse({"error": "Request body must be a JSON object."}, status=400)
    return body, None


def _error(message, status=400):
    return JsonResponse({"error": message}, status=status)


@login_required
def screen(request):
    return render(request, "pos/screen.html")


@login_required
@require_GET
def product_search(request):
    query = request.GET.get("q", "").strip()
    if not query:
        return JsonResponse({"results": []})
    if len(query) > 100:
        return _error("Search query is too long.")

    products = with_stock(Product.objects.filter(is_active=True).filter(
        Q(name__icontains=query) | Q(code__icontains=query) | Q(barcode__icontains=query)
    )).order_by("name")[:20]
    results = []
    for product in products:
        results.append({
            "id": product.pk,
            "name": product.name,
            "sku": product.code or "",
            "barcode": product.barcode or "",
            "price": str(product.sale_price),
            "mrp": str(product.mrp),
            "gst_rate": str(product.gst_rate),
            "stock": str(product.stock),
            "track_stock": product.track_stock,
        })
    return JsonResponse({"results": results})


@login_required
@require_POST
def checkout(request):
    body, error = _json_body(request)
    if error:
        return error

    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > MAX_CART_ITEMS:
        return _error(f"Provide between 1 and {MAX_CART_ITEMS} cart items.")

    payment_method = body.get("payment_method")
    if payment_method not in Sale.PaymentMethod.values:
        return _error("Choose a valid payment method.")

    requested_items = {}
    try:
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ValueError("Each cart item must be an object.")
            product_id = int(raw_item.get("id", raw_item.get("product_id")))
            if product_id <= 0 or product_id in requested_items:
                raise ValueError("Product IDs must be positive and unique.")
            quantity = Decimal(str(raw_item.get("qty", raw_item.get("quantity"))))
            if (
                not quantity.is_finite()
                or quantity <= 0
                or quantity > Decimal("999999999.999")
                or quantity != quantity.quantize(MILLI)
            ):
                raise ValueError("Quantities must be positive and have at most 3 decimal places.")
            discount = Decimal(str(raw_item.get("discount", "0")))
            if (
                not discount.is_finite()
                or discount < 0
                or discount != discount.quantize(CENT)
            ):
                raise ValueError("Item discounts must be zero or greater.")
            discount = discount.quantize(CENT)
            requested_items[product_id] = {"quantity": quantity, "discount": discount}
    except (TypeError, ValueError, InvalidOperation):
        return _error("One or more product quantities, IDs, or discounts are invalid.")

    with transaction.atomic():
        products = list(
            Product.objects.select_for_update()
            .filter(pk__in=requested_items, is_active=True)
            .order_by("pk")
        )
        if len(products) != len(requested_items):
            return _error("A product is no longer available. Refresh the cart and try again.", 409)

        sale_lines = []
        subtotal = discount_total = tax_total = ZERO
        for product in products:
            requested = requested_items[product.pk]
            quantity = requested["quantity"]
            unit_discount = requested["discount"]
            unit_price = product.sale_price
            if unit_discount > unit_price:
                return _error(f"Discount exceeds the price for {product.name}.")

            if product.track_stock:
                stock = product.movements.aggregate(total=Sum("quantity"))["total"] or ZERO
                if stock < quantity:
                    return _error(
                        f"Insufficient stock for {product.name} (available: {stock}).",
                        409,
                    )

            line_subtotal = _money(unit_price * quantity)
            line_discount = _money(unit_discount * quantity)
            line_tax = _money((line_subtotal - line_discount) * product.gst_rate / 100)
            line_total = line_subtotal - line_discount + line_tax
            subtotal += line_subtotal
            discount_total += line_discount
            tax_total += line_tax
            sale_lines.append({
                "product": product,
                "quantity": quantity,
                "unit_price": unit_price,
                "discount": line_discount,
                "total": line_total,
            })

        total = subtotal - discount_total + tax_total
        payment_details = body.get("payment_details") or {}
        if not isinstance(payment_details, dict):
            return _error("Payment details must be an object.")
        if payment_method == Sale.PaymentMethod.SPLIT:
            try:
                split_payments = {}
                for method in ("cash", "card", "upi"):
                    amount = Decimal(str(payment_details.get(method, "0")))
                    if (
                        not amount.is_finite()
                        or amount < ZERO
                        or amount != amount.quantize(CENT)
                    ):
                        raise ValueError("Invalid split amount.")
                    split_payments[method] = amount.quantize(CENT)
            except (InvalidOperation, TypeError, ValueError):
                return _error("Split payment amounts are invalid.")
            tendered = sum(split_payments.values(), ZERO)
            if tendered != total:
                return _error("Split payment amounts must equal the sale total.")
            payment_details = {key: str(value) for key, value in split_payments.items()}
        else:
            payment_details = {}
        maximum_total = Decimal("9999999999.99")
        if any(value > maximum_total for value in (subtotal, discount_total, tax_total, total)):
            return _error("Sale total exceeds the supported amount.")

        customer = None
        customer_id = body.get("customer_id")
        customer_name = body.get("customer_name", "")
        customer_phone = body.get("customer_phone", "")
        if not isinstance(customer_name, str) or not isinstance(customer_phone, str):
            return _error("Customer name and phone number must be text.")
        customer_name = customer_name.strip()
        customer_phone = customer_phone.strip()
        if customer_id:
            try:
                customer_id = int(customer_id)
            except (TypeError, ValueError):
                return _error("The selected customer ID is invalid.")
            if customer_id <= 0:
                return _error("The selected customer ID is invalid.")
            customer = Contact.objects.filter(
                pk=customer_id, is_customer=True, is_active=True
            ).first()
            if customer is None:
                return _error("The selected customer could not be found.")
        elif customer_name or customer_phone:
            if len(customer_name) > 200 or len(customer_phone) > 15:
                return _error("Customer name or phone number is too long.")
            customer = (
                Contact.objects.filter(phone=customer_phone, is_customer=True, is_active=True).first()
                if customer_phone else None
            )
            if customer is None:
                customer = Contact(name=customer_name or customer_phone, phone=customer_phone)
                try:
                    customer.full_clean()
                except ValidationError as exc:
                    return _error("; ".join(exc.messages))
                customer.save()

        sale = Sale.objects.create(
            cashier=request.user,
            customer=customer,
            subtotal=_money(subtotal),
            tax=_money(tax_total),
            discount=_money(discount_total),
            total=_money(total),
            payment_method=payment_method,
            payment_details=payment_details,
        )
        receipt_items = []
        for line in sale_lines:
            SaleItem.objects.create(
                sale=sale,
                product=line["product"],
                quantity=line["quantity"],
                unit_price=line["unit_price"],
                discount=line["discount"],
                total_amount=line["total"],
            )
            if line["product"].track_stock:
                StockMovement.objects.create(
                    product=line["product"],
                    quantity=-line["quantity"],
                    movement_type=StockMovement.Type.SALE,
                    reference=sale.order_number,
                    note=f"Sale {sale.order_number}",
                    created_by=request.user,
                )
            receipt_items.append({
                "id": line["product"].pk,
                "name": line["product"].name,
                "sku": line["product"].code or "",
                "quantity": str(line["quantity"]),
                "unit_price": str(line["unit_price"]),
                "discount": str(line["discount"]),
                "total": str(line["total"]),
                "stock": str(
                    line["product"].movements.aggregate(total=Sum("quantity"))["total"] or ZERO
                ),
            })

    return JsonResponse({
        "sale": {
            "id": sale.pk,
            "order_number": sale.order_number,
            "created_at": sale.created_at.isoformat(),
            "customer": customer.name if customer else "",
            "subtotal": str(sale.subtotal),
            "discount": str(sale.discount),
            "tax": str(sale.tax),
            "total": str(sale.total),
            "payment_method": sale.payment_method,
            "items": receipt_items,
        }
    }, status=201)

@login_required
@require_POST
def hold_sale(request):
    body, error = _json_body(request)
    if error:
        return error
    cart = body.get("items")
    if not isinstance(cart, list) or not cart or len(cart) > MAX_CART_ITEMS:
        return _error(f"Provide between 1 and {MAX_CART_ITEMS} cart items to hold.")
    if any(not isinstance(item, dict) or not item.get("id") for item in cart):
        return _error("Held cart contains an invalid product.")

    held = HeldSale.objects.create(
        cashier=request.user,
        cart=cart,
        customer_name=str(body.get("customer_name", ""))[:200],
        customer_phone=str(body.get("customer_phone", ""))[:15],
    )
    return JsonResponse({"id": held.pk, "created_at": held.created_at.isoformat()}, status=201)


@login_required
@require_GET
def held_sales(request):
    sales = HeldSale.objects.filter(cashier=request.user)[:20]
    return JsonResponse({
        "results": [{
            "id": held.pk,
            "created_at": held.created_at.isoformat(),
            "customer_name": held.customer_name,
            "customer_phone": held.customer_phone,
            "items": held.cart,
        } for held in sales]
    })


@login_required
@require_POST
def restore_held_sale(request, held_id):
    with transaction.atomic():
        held = HeldSale.objects.select_for_update().filter(
            pk=held_id, cashier=request.user
        ).first()
        if held is None:
            return _error("Held sale was not found.", 404)
        result = {
            "id": held.pk,
            "items": held.cart,
            "customer_name": held.customer_name,
            "customer_phone": held.customer_phone,
        }
        held.delete()
    return JsonResponse(result)
