"""Targeted offline tests for conservative fare selection in the local OCR."""

import unittest

from rimborso_voli import ocr_fallback


BASE = (
    "COMPAGNIA AEREA: RYANAIR",
    "NUMERO VOLO: FR1234",
    "ROMA FIUMICINO - CATANIA",
)


def receipt(*lines):
    return ocr_fallback._extract_texts({"pdf_ricevuta": "\n".join(lines)})


class FareSelectionTests(unittest.TestCase):
    def test_single_single_passenger_fare_is_used(self):
        values = receipt(*BASE, "1 X TARIFFA", "EUR 49.90")
        self.assertEqual(values["costo_tratta"], "49.90")

    def test_multiple_passenger_fare_is_ambiguous(self):
        values = receipt(*BASE, "2 X TARIFFA", "EUR 99.80")
        self.assertIsNone(values["costo_tratta"])

    def test_multiple_fare_rows_are_ambiguous(self):
        values = receipt(*BASE, "1 X TARIFFA", "EUR 49.90", "1 X TARIFFA", "EUR 60.00")
        self.assertIsNone(values["costo_tratta"])

    def test_multiple_amounts_on_the_fare_row_are_ambiguous(self):
        values = receipt(*BASE, "1 X TARIFFA EUR 49.90 EUR 60.00")
        self.assertIsNone(values["costo_tratta"])

    def test_fare_without_a_flight_is_not_used(self):
        values = receipt("RICEVUTA", "1 X TARIFFA", "EUR 49.90")
        self.assertIsNone(values["costo_tratta"])


if __name__ == "__main__":
    unittest.main()
