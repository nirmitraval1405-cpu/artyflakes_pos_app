
"""Print settings: the options, their defaults, and checking what the browser sends.

No Django imports here, so it is easy to test. storeconfig/models.py (PrintSettings) has one field per name below.
"""

# Font name -> CSS font stack. "" means "leave the template's own font".
FONTS = {
    "": "",
    "Arial Black": '"Arial Black", Arial, Helvetica, sans-serif',
    "Arial": "Arial, Helvetica, sans-serif",
    "Calibri": "Calibri, Carlito, Arial, sans-serif",
    "Georgia": "Georgia, 'Times New Roman', serif",
    "Times New Roman": "'Times New Roman', Times, serif",
    "Verdana": "Verdana, Geneva, sans-serif",
    "Tahoma": "Tahoma, Geneva, sans-serif",
    "Trebuchet MS": "'Trebuchet MS', Helvetica, sans-serif",
    "Segoe UI": "'Segoe UI', Arial, sans-serif",
    "Courier New": "'Courier New', Courier, monospace",
}

DARKNESS = {
    "normal": {"weight": 500, "stroke": 0},
    "bold": {"weight": 700, "stroke": 0.25},
    "extra": {"weight": 900, "stroke": 0.6},
}

# Words printed on the receipt, and the text used when nothing custom is set.
LABEL_DEFAULTS = {
    "invoice": "Invoice", "order_no": "Order no", "date": "Date", "cashier": "Cashier", "customer": "Customer",
    "n_items": "Items", "n_qty": "Qty", "subtotal": "Subtotal", "discount": "Discount", "total": "TOTAL",
    "gst_included": "Includes GST", "change": "Change", "note": "Note",
}

# name -> default
BOOLS = {
    "r_tax_totals": True,      # receipt: "Includes GST" line
    "r_item_gst": False,       # receipt: GST rate beside each item
    "r_unit": False,           # receipt: unit after the quantity (2 pcs)
    "r_short_no": False,       # receipt: 0012 instead of AF-2026-0012
    "r_order_no": False,       # receipt: today's bill number
    "r_cust_name": True, "r_cust_code": False, "r_cust_gstin": False, "r_cust_address": False,
    "i_a5": False,             # invoice on A5 paper
    "i_col_tax": True, "i_col_discount": True,
    "i_cust_gstin": True, "i_cust_code": False, "i_cust_phone": True, "i_cust_email": False,
    "i_payments": True,
}
# name -> (default, allowed values)
CHOICES = {
    "receipt_width_mm": (80, [58, 80]),
    "r_darkness": ("extra", list(DARKNESS)),
    "r_summary": ("none", ["none", "count", "qty"]),
    "font": ("Arial Black", list(FONTS)),
    "i_footer_align": ("center", ["left", "center"]),
}
# name -> (default, lowest, highest)
INTS = {"r_decimals": (2, 0, 3), "r_font_size": (100, 80, 150)}
# name -> longest allowed text
TEXTS = {"i_title": 40, "i_footer": 300}

FIELD_NAMES = [*BOOLS, *CHOICES, *INTS, *TEXTS]
LABEL_MAX = 30


def clean_payload(data):
    """Checks a dict sent by the settings dialog. Returns only the valid fields. Raises ValueError with a message."""
    if not isinstance(data, dict):
        raise ValueError("Invalid request.")
    clean = {}
    for name in BOOLS:
        if name in data:
            if not isinstance(data[name], bool):
                raise ValueError("Invalid on/off value.")
            clean[name] = data[name]
    for name, (_, allowed) in CHOICES.items():
        if name in data:
            if data[name] not in allowed or isinstance(data[name], bool):
                raise ValueError("Pick one of the listed options.")
            clean[name] = data[name]
    for name, (_, low, high) in INTS.items():
        if name in data:
            value = data[name]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"Decimal places must be between {low} and {high}.")
            clean[name] = value
    for name, longest in TEXTS.items():
        if name in data:
            clean[name] = str(data[name] or "").strip()[:longest]
    if "labels" in data:
        if not isinstance(data["labels"], dict):
            raise ValueError("Invalid receipt text.")
        labels = {}
        for key, value in data["labels"].items():
            text = str(value or "").strip()[:LABEL_MAX]
            if key in LABEL_DEFAULTS and text and text != LABEL_DEFAULTS[key]:
                labels[key] = text
        clean["labels"] = labels
    return clean