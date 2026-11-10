from django.test import SimpleTestCase

from . import printing
from .models import PrintSettings


class PrintingSchemaTests(SimpleTestCase):
    def test_every_option_has_exactly_one_model_field(self):
        model_fields = {f.name for f in PrintSettings._meta.get_fields()}
        for name in printing.FIELD_NAMES + ["labels"]:
            self.assertIn(name, model_fields)

    def test_defaults_match_between_schema_and_model(self):
        s = PrintSettings()
        for name, default in printing.BOOLS.items():
            self.assertEqual(getattr(s, name), default, name)
        for name, (default, _) in printing.CHOICES.items():
            self.assertEqual(getattr(s, name), default, name)
        for name, (default, _, _) in printing.INTS.items():
            self.assertEqual(getattr(s, name), default, name)

    def test_valid_payload_is_cleaned(self):
        clean = printing.clean_payload({"r_decimals": 0, "r_summary": "qty", "i_title": "  Bill ", "receipt_width_mm": 58})
        self.assertEqual(clean, {"r_decimals": 0, "r_summary": "qty", "i_title": "Bill", "receipt_width_mm": 58})

    def test_bad_values_are_refused(self):
        for bad in ({"r_decimals": 4}, {"r_decimals": True}, {"r_summary": "all"}, {"i_a5": "yes"},
                    {"receipt_width_mm": 70}, {"font": "Comic Sans"}, {"labels": []}):
            with self.assertRaises(ValueError, msg=str(bad)):
                printing.clean_payload(bad)

    def test_labels_keep_only_known_changed_text(self):
        clean = printing.clean_payload({"labels": {"total": "Kul", "invoice": "Invoice", "bogus": "x", "change": ""}})
        self.assertEqual(clean["labels"], {"total": "Kul"})

    def test_context_has_what_the_bill_templates_use(self):
        ctx = PrintSettings(r_decimals=0, receipt_width_mm=58, font="Arial", i_a5=True,
                            labels={"total": "Kul"}).as_context()
        self.assertEqual((ctx["money_fmt"], ctx["receipt_inner_mm"], ctx["i_sheet_mm"]), ("0g", 50, 118))
        self.assertEqual(ctx["labels"]["total"], "Kul")
        self.assertEqual(ctx["labels"]["subtotal"], "Subtotal")
        self.assertIn("Arial", ctx["font_css"])