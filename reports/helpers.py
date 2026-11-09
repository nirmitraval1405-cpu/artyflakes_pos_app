from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone

from dashboard.views import inr
from inventory.models import Category, Product
from inventory.stock import qty_fmt
from people.models import Contact

PAGE_CAP = 5000       # rows shown on the page
CSV_CAP = 50000       # rows in the Excel (CSV) file
TWO = Decimal("0.01")
NUM_KINDS = {"money", "qty", "int", "pct"}
FILTER_LABELS = {"product": "Product", "group": "Product group", "party": "Customers & suppliers", "user": "User"}


def safe(value):
    """Stops spreadsheet apps from running a cell that starts with = + - @"""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _to_date(text):
    text = (text or "").strip()
    if not text:
        return None, False
    try:
        return date.fromisoformat(text), False
    except ValueError:
        return None, True


def _to_int(text):
    text = (text or "").strip()
    return int(text) if text.isdigit() else None


class Params:
    """The filters from the page, checked. Bad dates or ids never crash a report."""

    def __init__(self, request):
        g = request.GET
        today = timezone.localdate()
        self.notes = []

        d_from, bad_from = _to_date(g.get("date_from"))
        d_to, bad_to = _to_date(g.get("date_to"))
        if bad_from or bad_to:
            self.notes.append("A date was not valid, so the default was used.")
        self.date_to = d_to or today
        self.date_from = d_from or today.replace(day=1)
        if self.date_from > self.date_to:
            self.date_from, self.date_to = self.date_to, self.date_from
            self.notes.append("The From date was after the To date, so they were swapped.")

        self.product = _to_int(g.get("product"))
        self.group = _to_int(g.get("group"))
        self.party = _to_int(g.get("party"))
        self.user = _to_int(g.get("user"))
        self.supplied = {name for name in FILTER_LABELS if getattr(self, name)}

    def chips(self, uses=(), dates=True, party_label="Customer"):
        out = []
        if dates:
            out.append(f"Dates: {self.date_from:%d-%m-%Y} to {self.date_to:%d-%m-%Y}")
        if "product" in uses and self.product:
            obj = Product.objects.filter(pk=self.product).first()
            if obj:
                out.append(f"Product: {obj.name}")
        if "group" in uses and self.group:
            obj = Category.objects.filter(pk=self.group).first()
            if obj:
                out.append(f"Group: {obj.name}")
        if "party" in uses and self.party:
            obj = Contact.objects.filter(pk=self.party).first()
            if obj:
                out.append(f"{party_label}: {obj.name}")
        if "user" in uses and self.user:
            obj = get_user_model().objects.filter(pk=self.user).first()
            if obj:
                out.append(f"User: {obj.get_full_name() or obj.username}")
        return out


# ---------- Turning values into page text and file values ----------
def _page(kind, v):
    if v is None or v == "":
        return ""
    if kind == "money":
        return inr(v)
    if kind == "qty":
        return qty_fmt(v)
    if kind == "int":
        return str(int(v))
    if kind == "pct":
        return f"{Decimal(v):.2f}%"
    if kind == "date":
        return v.strftime("%d-%m-%Y")
    if kind == "datetime":
        return timezone.localtime(v).strftime("%d-%m-%Y %H:%M")
    return str(v)


def _csv(kind, v):
    if v is None or v == "":
        return ""
    if kind in ("money", "pct"):
        return Decimal(v).quantize(TWO)
    if kind == "qty":
        return qty_fmt(v)
    if kind == "int":
        return int(v)
    if kind == "date":
        return v.isoformat()
    if kind == "datetime":
        return timezone.localtime(v).strftime("%Y-%m-%d %H:%M")
    return safe(v)


@dataclass
class Col:
    label: str
    key: str
    kind: str = "text"      # text, money, qty, int, pct, date, datetime


class Report:
    def __init__(self, cols, records, chips=None, totals=(), totals_label="Total", totals_where=None, notes=None):
        self.cols = cols
        self.records = records
        self.chips = chips or []
        self.totals = totals
        self.totals_label = totals_label
        self.totals_where = totals_where
        self.notes = notes or []

    @property
    def headers(self):
        return [(c.label, "num" if c.kind in NUM_KINDS else "") for c in self.cols]

    def _totals(self):
        if not self.totals or not self.records:
            return None
        rows = [r for r in self.records if self.totals_where(r)] if self.totals_where else self.records
        return {key: sum((r.get(key) or 0 for r in rows), 0) for key in self.totals}

    def page_rows(self, limit=None):
        limit = PAGE_CAP if limit is None else limit
        shown = self.records[:limit]
        rows = [
            [(_page(c.kind, r.get(c.key)), "num" if c.kind in NUM_KINDS else "") for c in self.cols]
            for r in shown
        ]
        return rows, len(self.records) > limit

    def page_total_row(self):
        t = self._totals()
        if t is None:
            return None
        cells = []
        for i, c in enumerate(self.cols):
            if c.key in t:
                cells.append((_page(c.kind, t[c.key]), "num"))
            else:
                cells.append((self.totals_label if i == 0 else "", ""))
        return cells

    def csv_rows(self, limit=None):
        limit = CSV_CAP if limit is None else limit
        shown = self.records[:limit]
        return [[_csv(c.kind, r.get(c.key)) for c in self.cols] for r in shown], len(self.records) > limit

    def csv_total_row(self):
        t = self._totals()
        if t is None:
            return None
        return [
            _csv(c.kind, t[c.key]) if c.key in t else (self.totals_label if i == 0 else "")
            for i, c in enumerate(self.cols)
        ]