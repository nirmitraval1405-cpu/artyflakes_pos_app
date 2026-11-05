import hashlib
import secrets
from io import BytesIO

from barcode import get as get_barcode
from barcode.writer import ImageWriter
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage


BARCODE_FORMAT = "code128"
NUMERIC_BARCODE_LENGTH = 12


def _normalize_barcode(value):
    barcode_value = str(value or "").strip()
    if not barcode_value:
        raise ValidationError("Barcode cannot be empty.")
    if len(barcode_value) > 64:
        raise ValidationError("Barcode must be 64 characters or fewer.")
    if any(ord(character) < 32 or ord(character) > 126 for character in barcode_value):
        raise ValidationError("Barcode must contain printable ASCII characters only.")
    return barcode_value


def validate_barcode(value, *, exclude_pk=None):
    """Normalize and ensure a barcode is valid and not assigned to another product."""
    barcode_value = _normalize_barcode(value)

    from inventory.models import Product

    products = Product.objects.filter(barcode=barcode_value)
    if exclude_pk is not None:
        products = products.exclude(pk=exclude_pk)
    if products.exists():
        raise ValidationError("This barcode is already assigned to another product.")
    return barcode_value


def get_unique_barcode(sku=None):
    """Prefer a valid, unused SKU; otherwise generate a unique 12-digit CODE128 value."""
    from inventory.models import Product

    if sku:
        try:
            sku_value = _normalize_barcode(sku)
        except ValidationError:
            sku_value = None
        if sku_value and not Product.objects.filter(barcode=sku_value).exists():
            return sku_value

    for _ in range(100):
        candidate = f"{secrets.randbelow(10**NUMERIC_BARCODE_LENGTH):0{NUMERIC_BARCODE_LENGTH}d}"
        if not Product.objects.filter(barcode=candidate).exists():
            return candidate

    raise RuntimeError("Unable to generate a unique barcode after 100 attempts.")


def generate_barcode(sku=None):
    """Generate an unused CODE128 barcode, using the SKU when it is available."""
    return get_unique_barcode(sku=sku)


def get_barcode_image_name(value):
    barcode_value = _normalize_barcode(value)
    digest = hashlib.sha256(barcode_value.encode("ascii")).hexdigest()
    return f"barcodes/{digest}.png"


def create_barcode_image(value):
    """Create and store a CODE128 PNG; return its storage-relative filename."""
    barcode_value = _normalize_barcode(value)
    image = get_barcode(BARCODE_FORMAT, barcode_value, writer=ImageWriter())
    image_data = BytesIO()
    image.write(image_data)

    image_name = get_barcode_image_name(barcode_value)
    if default_storage.exists(image_name):
        default_storage.delete(image_name)
    return default_storage.save(image_name, ContentFile(image_data.getvalue()))
