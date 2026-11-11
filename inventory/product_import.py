import csv
import io
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .models import Brand, Category, Product, StockMovement
from .stock import current_stock

MAX_IMPORT_ROWS = 10000
IMPORT_FIELDS = (
    ("code", "Code"),
    ("name", "Name"),
    ("category", "Group"),
    ("brand", "Brand"),
    ("barcode", "Barcode"),
    ("cost_price", "Cost"),
    ("sale_price", "Sale price"),
    ("mrp", "MRP"),
    ("gst_rate", "GST %"),
    ("stock", "Stock"),
)

HEADER_ALIASES = {
    "code": "code",
    "sku": "code",
    "name": "name",
    "group": "category",
    "category": "category",
    "brand": "brand",
    "barcode": "barcode",
    "cost": "cost_price",
    "cost price": "cost_price",
    "sale price": "sale_price",
    "sale_price": "sale_price",
    "mrp": "mrp",
    "gst": "gst_rate",
    "gst %": "gst_rate",
    "gst rate": "gst_rate",
    "stock": "stock",
    "quantity": "stock",
}


def parse_product_csv(text):
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        headers = next(reader)
    except StopIteration:
        raise ValueError("The CSV file is empty.") from None

    headers = [str(header).strip() for header in headers]
    if not headers or not any(headers):
        raise ValueError("The CSV file must contain a header row.")

    rows = []
    for row in reader:
        if not any(str(value).strip() for value in row):
            continue
        if len(rows) >= MAX_IMPORT_ROWS:
            raise ValueError(f"The CSV file cannot contain more than {MAX_IMPORT_ROWS} product rows.")
        rows.append(row)

    if not rows:
        raise ValueError("The CSV file has no product rows.")

    mapping = {}
    used = set()
    for index, header in enumerate(headers):
        key = HEADER_ALIASES.get(header.casefold())
        if key and key not in used:
            mapping[index] = key
            used.add(key)

    return headers, rows, mapping


def _decimal(value, label, max_digits, decimal_places, maximum=None):
    if not value:
        return None
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{label} must be a number.") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"{label} must be zero or greater.")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} cannot be greater than {maximum}.")
    try:
        quantized = number.quantize(Decimal(1).scaleb(-decimal_places))
    except InvalidOperation:
        raise ValueError(f"{label} is too large.") from None
    if len(quantized.as_tuple().digits) > max_digits:
        raise ValueError(f"{label} is too large.")
    return quantized


def _mapped_value(row, mapping, field):
    for index, target in mapping.items():
        if target == field:
            return row[index].strip() if index < len(row) else ""
    return None


def _match_product(code, barcode):
    by_code = Product.objects.filter(code__iexact=code).first() if code else None
    by_barcode = Product.objects.filter(barcode__iexact=barcode).first() if barcode else None
    if by_code and by_barcode and by_code.pk != by_barcode.pk:
        raise ValueError("Code and Barcode match different existing products.")
    return by_code or by_barcode


def _get_or_create_named(model, value, label):
    if not value:
        return None
    if len(value) > model._meta.get_field("name").max_length:
        raise ValueError(f"{label} is too long.")
    existing = model.objects.filter(name__iexact=value).first()
    return existing or model.objects.create(name=value)


def import_product_rows(rows, mapping, matching_behavior, user):
    if matching_behavior not in ("update", "skip"):
        raise ValueError("Choose how to handle rows that match existing products.")
    mapped_fields = list(mapping.values())
    if len(mapped_fields) != len(set(mapped_fields)):
        raise ValueError("Map each product field at most once.")
    if "name" not in mapped_fields:
        raise ValueError("Map a CSV column to Name before importing.")

    result = {"created": 0, "updated": 0, "skipped": 0, "failed": 0, "errors": []}
    for row_number, row in enumerate(rows, start=2):
        try:
            values = {
                field: _mapped_value(row, mapping, field)
                for field, _ in IMPORT_FIELDS
            }
            code = values["code"]
            if code and len(code) > 30:
                raise ValueError("Code cannot be longer than 30 characters.")
            barcode = values["barcode"]
            if barcode and len(barcode) > 64:
                raise ValueError("Barcode cannot be longer than 64 characters.")

            product = _match_product(code or "", barcode or "")
            if product and matching_behavior == "skip":
                result["skipped"] += 1
                continue

            name = values["name"]
            if not name:
                raise ValueError("Name is required.")
            if len(name) > 200:
                raise ValueError("Name cannot be longer than 200 characters.")

            cost = _decimal(values["cost_price"], "Cost", 10, 2)
            sale = _decimal(values["sale_price"], "Sale price", 10, 2)
            mrp = _decimal(values["mrp"], "MRP", 10, 2)
            gst = _decimal(values["gst_rate"], "GST %", 5, 2, Decimal("100"))
            stock = _decimal(values["stock"], "Stock", 12, 3)

            with transaction.atomic():
                created = product is None
                product = product or Product(name=name)
                product.name = name
                if code:
                    product.code = code
                if barcode:
                    product.barcode = barcode or None
                if cost is not None:
                    product.cost_price = cost
                if sale is not None:
                    product.sale_price = sale
                if mrp is not None:
                    product.mrp = mrp
                if gst is not None:
                    product.gst_rate = gst
                if sale is not None and mrp is not None and sale > mrp:
                    raise ValueError("Sale price cannot be higher than MRP.")

                group = values["category"]
                if group:
                    product.category = _get_or_create_named(Category, group, "Group")
                brand = values["brand"]
                if brand:
                    product.brand = _get_or_create_named(Brand, brand, "Brand")
                product.full_clean(exclude=["image"])
                product.save()

                if stock is not None:
                    if not product.track_stock:
                        raise ValueError("This product does not track stock.")
                    old_stock = current_stock(product) if not created else Decimal("0")
                    delta = stock - old_stock
                    if delta:
                        StockMovement.objects.create(
                            product=product,
                            quantity=delta,
                            movement_type=(
                                StockMovement.Type.OPENING if created else StockMovement.Type.ADJUSTMENT
                            ),
                            source=StockMovement.Source.POS,
                            note=f"Product CSV import (row {row_number})",
                            created_by=user,
                        )

            result["created" if created else "updated"] += 1
        except (ValueError, ValidationError, IntegrityError) as exc:
            if isinstance(exc, ValidationError):
                message = "; ".join(exc.messages)
            else:
                message = str(exc)
            result["failed"] += 1
            if len(result["errors"]) < 20:
                result["errors"].append(f"Row {row_number}: {message}")

    return result
