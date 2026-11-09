from decimal import ROUND_HALF_UP, Decimal

TWO = Decimal("0.01")


def split_tax(tax):
    """Half and half (CGST + SGST). The invoice page and the GST reports both use this, so they always agree."""
    cgst = (tax / 2).quantize(TWO, rounding=ROUND_HALF_UP)
    return cgst, tax - cgst