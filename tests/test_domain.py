from decimal import Decimal
from io import BytesIO
import unittest

from PIL import Image

from domain import (
    MAX_UPLOAD_BYTES, ValidationError, normalize_fields, parse_amount,
    parse_extraction, prepare_document,
)


class DomainTests(unittest.TestCase):
    def test_amount_locales_and_missing_values(self):
        for raw in ("1234,56", "1.234,56", "1,234.56", "€ 1234.56", 1234.56):
            with self.subTest(raw=raw):
                self.assertEqual(parse_amount(raw), Decimal("1234.56"))
        self.assertIsNone(parse_amount(""))
        self.assertIsNone(parse_amount(None))

    def test_reject_ambiguous_or_unsafe_amounts(self):
        for raw in ("1.234", "1,234", "nan", "inf", "-12", "0", "100001", "abc", True, {}, "1e3"):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                parse_amount(raw)

    def test_draft_can_have_no_extracted_fields(self):
        values = normalize_fields({})
        self.assertIsNone(values["costo_tratta"])
        self.assertIsNone(values["data_volo"])

    def test_dates_validate_and_normalize(self):
        values = normalize_fields({"data_acquisto": "01/09/2026", "data_volo": "2026-10-01"})
        self.assertEqual(values["data_acquisto"], "2026-09-01")
        for values in ({"data_volo": "31/02/2026"}, {"data_acquisto": "02/10/2026", "data_volo": "01/10/2026"}):
            with self.assertRaises(ValidationError):
                normalize_fields(values)

    def test_ai_output_is_untrusted(self):
        for text in ('[]', 'null', 'not json', '{"user_id":"another-user"}', '{"numero_volo": []}', '{"numero_volo": false}', '{"costo_tratta": false}', '{"numero_volo": ["AB12"]}', '{"costo_tratta": "nan"}'):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_extraction(text)
        self.assertIsNone(parse_extraction('{"costo_tratta": "0"}')["costo_tratta"])

    def test_valid_image_reencoded_to_pdf_without_metadata(self):
        buffer = BytesIO()
        Image.new("RGBA", (64, 64), (255, 255, 255, 0)).save(buffer, "PNG")
        document = prepare_document(buffer.getvalue() + b"hidden trailing payload")
        self.assertTrue(document.pdf.startswith(b"%PDF-"))
        self.assertNotIn(b"hidden trailing payload", document.pdf)
        self.assertEqual(document.image.mode, "RGB")

    def test_reject_non_images_oversized_and_excessive_pixels(self):
        for data in (b"<script>alert(1)</script>", b"x" * (MAX_UPLOAD_BYTES + 1)):
            with self.assertRaises(ValidationError):
                prepare_document(data)
        buffer = BytesIO()
        Image.new("1", (4000, 4000)).save(buffer, "PNG")
        with self.assertRaises(ValidationError):
            prepare_document(buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
