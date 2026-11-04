from django.utils.text import slugify

# (section title, [(report title, ready?)])
_RAW = [
    ("Inventory", [("Product list", True)]),
    ("Sales", [
        ("Products", False),
        ("Product groups", False),
        ("Customers", False),
        ("Tax rates", False),
        ("Users", False),
        ("Item list", False),
        ("Payment types", False),
        ("Payment types by users", False),
        ("Payment types by customers", False),
        ("Refunds", False),
        ("Invoice list", False),
        ("Daily sales", False),
        ("Hourly sales", False),
        ("Hourly sales by product groups", False),
        ("Table or order number", False),
        ("Profit & margin", False),
        ("Unpaid sales", False),
        ("Starting cash entries", False),
        ("Voided items", False),
        ("Collected credit payments", False),
        ("Discounts granted", False),
        ("Items discounts", False),
        ("Stock movement", False),
    ]),
    ("Purchase", [
        ("Products", False),
        ("Suppliers", False),
        ("Payment types", False),
        ("Payment types by supplier", False),
        ("Unpaid purchase", False),
        ("Purchase discounts", False),
        ("Purchased items discounts", False),
        ("Purchase invoice list", False),
        ("Tax rates", False),
        ("Expiration date", False),
    ]),
    ("Stock return", [("Products", False)]),
    ("Loss and damage", [("Products", False)]),
    ("Finance", [("Transaction history", False)]),
    ("Stock control", [("Reorder product list", False), ("Low stock warning", False)]),
]

SECTIONS = []
REPORTS = {}

for section_title, items in _RAW:
    reports = []
    for title, ready in items:
        report = {
            "slug": slugify(f"{section_title} {title}"),
            "title": title,
            "section": section_title,
            "ready": ready,
        }
        reports.append(report)
        REPORTS[report["slug"]] = report
    SECTIONS.append({"title": section_title, "reports": reports})