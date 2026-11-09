from decimal import Decimal

from django.db.models import Max, Sum

from inventory.models import Product, StockMovement
from inventory.stock import low_stock, stock_status, with_stock

from .helpers import Col, Report
from .registry import register

ZERO = Decimal("0")


# ---------- Product list (existing report) ----------
@register("Inventory", "Product list", uses=("product", "group"))
def product_list(p):
    qs = Product.objects.select_related("category").order_by("name")
    if p.group:
        qs = qs.filter(category_id=p.group)
    if p.product:
        qs = qs.filter(pk=p.product)
    records = [
        {
            "code": x.code or "", "name": x.name, "group": x.category.name if x.category else "", "barcode": x.barcode or "",
            "unit": x.unit, "cost": x.cost_price, "price": x.sale_price, "mrp": x.mrp,
            "gst": x.gst_rate, "status": "Active" if x.is_active else "Inactive",
        }
        for x in qs
    ]
    cols = [
        Col("Code", "code"), Col("Name", "name"), Col("Group", "group"), Col("Barcode", "barcode"), Col("Unit", "unit"),
        Col("Cost", "cost", "money"), Col("Sale price", "price", "money"), Col("MRP", "mrp", "money"),
        Col("GST %", "gst", "pct"), Col("Status", "status"),
    ]
    return Report(cols, records, p.chips(("product", "group"), dates=False))


# ---------- Low stock warning ----------
def _low(p):
    qs = low_stock(with_stock(Product.objects.select_related("category")))
    if p.group:
        qs = qs.filter(category_id=p.group)
    if p.product:
        qs = qs.filter(pk=p.product)
    return qs.order_by("stock", "name")


@register("Stock control", "Low stock warning", uses=("product", "group"))
def low_stock_report(p):
    records = [
        {
            "code": x.code or "", "name": x.name, "group": x.category.name if x.category else "",
            "stock": x.stock, "level": Decimal(x.min_stock), "status": stock_status(x)[1],
        }
        for x in _low(p)
    ]
    cols = [
        Col("Code", "code"), Col("Product", "name"), Col("Group", "group"), Col("In stock", "stock", "qty"),
        Col("Low stock level", "level", "qty"), Col("Status", "status"),
    ]
    notes = ["Shows active, stock-tracked products that are out of stock or at or below their low-stock level."]
    return Report(cols, records, p.chips(("product", "group"), dates=False), notes=notes)


# ---------- Reorder product list ----------
@register("Stock control", "Reorder product list", uses=("product", "group"))
def reorder_report(p):
    records = []
    for x in _low(p):
        short = max(Decimal(x.min_stock) - x.stock, ZERO)
        records.append({
            "code": x.code or "", "name": x.name, "group": x.category.name if x.category else "",
            "stock": x.stock, "level": Decimal(x.min_stock), "short": short, "cost": x.cost_price, "est": short * x.cost_price,
        })
    cols = [
        Col("Code", "code"), Col("Product", "name"), Col("Group", "group"), Col("In stock", "stock", "qty"),
        Col("Low stock level", "level", "qty"), Col("Order to reach the level", "short", "qty"),
        Col("Last cost", "cost", "money"), Col("Estimated cost", "est", "money"),
    ]
    notes = ["'Order to reach the level' is the low-stock level minus what you have. Products with no level set show 0, so set one on the Products page."]
    return Report(cols, records, p.chips(("product", "group"), dates=False), totals=("est",), notes=notes)


# ---------- Loss and damage ----------
@register("Loss and damage", "Products", uses=("product", "group"))
def loss_and_damage(p):
    qs = StockMovement.objects.filter(
        movement_type=StockMovement.Type.DAMAGE, created_at__date__gte=p.date_from, created_at__date__lte=p.date_to
    )
    if p.product:
        qs = qs.filter(product_id=p.product)
    if p.group:
        qs = qs.filter(product__category_id=p.group)
    rows = (
        qs.order_by().values("product_id")
        .annotate(t_name=Max("product__name"), t_code=Max("product__code"), t_qty=Sum("quantity"), t_cost=Max("product__cost_price"))
        .order_by("t_name")
    )
    records = [
        {"code": r["t_code"] or "", "name": r["t_name"], "qty": -r["t_qty"], "cost": r["t_cost"], "value": -r["t_qty"] * r["t_cost"]}
        for r in rows
    ]
    cols = [
        Col("Code", "code"), Col("Product", "name"), Col("Qty lost", "qty", "qty"),
        Col("Cost price", "cost", "money"), Col("Value lost", "value", "money"),
    ]
    notes = ["Valued at each product's current cost price."]
    return Report(cols, records, p.chips(("product", "group")), totals=("qty", "value"), notes=notes)


# ---------- Stock return ----------
@register("Stock return", "Products", uses=("product", "group"))
def stock_return_products(p):
    qs = StockMovement.objects.filter(
        movement_type=StockMovement.Type.PURCHASE_RETURN,
        created_at__date__gte=p.date_from,
        created_at__date__lte=p.date_to,
    )
    if p.product:
        qs = qs.filter(product_id=p.product)
    if p.group:
        qs = qs.filter(product__category_id=p.group)
    rows = (
        qs.order_by().values("product_id")
        .annotate(
            t_name=Max("product__name"),
            t_code=Max("product__code"),
            t_qty=Sum("quantity"),
            t_cost=Max("product__cost_price"),
        )
        .order_by("t_name")
    )
    records = [
        {
            "code": row["t_code"] or "",
            "name": row["t_name"],
            "qty": -row["t_qty"],
            "cost": row["t_cost"],
            "value": -row["t_qty"] * row["t_cost"],
        }
        for row in rows
    ]
    cols = [
        Col("Code", "code"), Col("Product", "name"), Col("Qty returned", "qty", "qty"),
        Col("Cost price", "cost", "money"), Col("Value returned", "value", "money"),
    ]
    notes = [
        "Uses existing purchase-return stock movements, which currently record purchase cancellation reversals. These do not confirm a physical return to the supplier.",
        "Valued at each product's current cost price.",
    ]
    return Report(cols, records, p.chips(("product", "group")), totals=("qty", "value"), notes=notes)


# ---------- Stock movement ----------
@register("Sales", "Stock movement", uses=("product", "group"))
def stock_movement(p):
    moves = (
        StockMovement.objects.select_related("product", "created_by")
        .filter(created_at__date__gte=p.date_from, created_at__date__lte=p.date_to)
        .order_by("created_at", "id")
    )
    if p.product:
        moves = moves.filter(product_id=p.product)
    if p.group:
        moves = moves.filter(product__category_id=p.group)

    records, running = [], None
    if p.product:
        running = (
            StockMovement.objects.filter(product_id=p.product, created_at__date__lt=p.date_from)
            .aggregate(t=Sum("quantity"))["t"] or ZERO
        )
        records.append({"when": None, "product": "", "type": "Balance before this period", "qty": None,
                        "ref": "", "note": "", "by": "", "balance": running})
    for m in moves:
        row = {
            "when": m.created_at, "product": m.product.name, "type": m.get_movement_type_display(), "qty": m.quantity,
            "ref": m.reference, "note": m.note, "by": m.created_by.username if m.created_by else "",
        }
        if running is not None:
            running += m.quantity
            row["balance"] = running
        records.append(row)

    cols = [
        Col("Date and time", "when", "datetime"), Col("Product", "product"), Col("Type", "type"),
        Col("Quantity", "qty", "qty"), Col("Reference", "ref"), Col("Note", "note"), Col("By", "by"),
    ]
    if p.product:
        cols.append(Col("Balance", "balance", "qty"))
    notes = [] if p.product else ["Choose a product in the filter to see a running balance."]
    return Report(cols, records, p.chips(("product", "group")), totals=("qty",) if p.product else (), notes=notes)