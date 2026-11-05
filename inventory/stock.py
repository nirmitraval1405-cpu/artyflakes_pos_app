from decimal import Decimal

from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce

from .models import Product

ZERO = Decimal("0")


def qty_fmt(value):
    """12.500 -> '12.5', 100.000 -> '100'"""
    return f"{Decimal(value).normalize():f}"


def with_stock(qs=None):
    """Adds a `stock` number (sum of movements) to every product."""
    qs = Product.objects.all() if qs is None else qs
    return qs.annotate(
        stock=Coalesce(
            Sum("movements__quantity"), Value(ZERO),
            output_field=DecimalField(max_digits=14, decimal_places=3),
        )
    )


def low_stock(qs):
    """Tracked, active products that are out of stock or at/below their low-stock level.
    `qs` must come from with_stock()."""
    return qs.filter(track_stock=True, is_active=True).filter(
        Q(stock__lte=0) | Q(min_stock__gt=0, stock__lte=F("min_stock"))
    )


def current_stock(product):
    return product.movements.aggregate(total=Sum("quantity"))["total"] or ZERO


def stock_status(p):
    """p must come from with_stock(). Returns (css kind, label)."""
    if not p.track_stock:
        return "na", "Not tracked"
    if p.stock <= 0:
        return "out", "Out of stock"
    if p.min_stock and p.stock <= p.min_stock:
        return "low", "Low"
    return "ok", "OK"