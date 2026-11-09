from django.utils.text import slugify

# (section, [report titles]). A report is "ready" when a builder is registered for it.
_RAW = [
    ("Inventory", ["Product list"]),
    ("Sales", [
        "Daily sales", "Sales by product", "Sales by payment type", "Bill-wise sales with GST",
        "GST summary by rate", "Sales returns", "Voided items", "Collected credit payments",
        "Discounts granted", "Items discounts", "Stock movement",
    ]),
    ("Purchase", [
        "Products", "Suppliers", "Payment types", "Payment types by supplier", "Unpaid purchase",
        "Purchase discounts", "Purchased items discounts", "Purchase invoice list", "Tax rates", "Expiration date",
    ]),
    ("Stock return", ["Products"]),
    ("Loss and damage", ["Products"]),
    ("Finance", ["Transaction history", "Profit and loss"]),
    ("Stock control", ["Reorder product list", "Low stock warning"]),
]

BUILDERS = {}   # slug -> function(params) -> Report
USES = {}       # slug -> the filters that report understands


def slug_for(section, title):
    return slugify(f"{section} {title}")


def register(section, title, uses=()):
    def decorator(fn):
        slug = slug_for(section, title)
        BUILDERS[slug] = fn
        USES[slug] = set(uses)
        return fn
    return decorator


def get_sections():
    from . import builders_purchase, builders_sales, builders_stock  # noqa: F401  (importing registers them)

    sections, reports = [], {}
    for section_title, titles in _RAW:
        items = []
        for title in titles:
            slug = slug_for(section_title, title)
            report = {"slug": slug, "title": title, "section": section_title, "ready": slug in BUILDERS}
            items.append(report)
            reports[slug] = report
        sections.append({"title": section_title, "reports": items})
    return sections, reports