"""Conservative, offline OCR fallback for temporarily unavailable Gemini calls."""

from datetime import date
from io import BytesIO
import re
import subprocess
import unicodedata

from domain import normalize_fields


class OcrUnavailable(RuntimeError):
    """The local OCR engine could not process an uploaded image."""


AIRPORTS = {
    "FCO": ("FCO", "FIUMICINO", "ROMA FIUMICINO"),
    "CIA": ("CIA", "CIAMPINO", "ROMA CIAMPINO"),
    "CTA": ("CTA", "CATANIA"),
    "PMO": ("PMO", "PALERMO"),
    "TPS": ("TPS", "TRAPANI"),
    "NAP": ("NAP", "NAPOLI"),
    "MXP": ("MXP", "MALPENSA", "MILANO MALPENSA"),
    "LIN": ("LIN", "LINATE", "MILANO LINATE"),
    "BGY": ("BGY", "BERGAMO"),
    "BLQ": ("BLQ", "BOLOGNA"),
    "PSA": ("PSA", "PISA"),
    "TRN": ("TRN", "TORINO"),
    "VCE": ("VCE", "VENEZIA"),
    "FLR": ("FLR", "FIRENZE"),
    "CAG": ("CAG", "CAGLIARI"),
    "OLB": ("OLB", "OLBIA"),
}
AIRLINES = {"FR": "Ryanair", "AZ": "ITA Airways", "U2": "easyJet", "W6": "Wizz Air", "VY": "Vueling"}
FLIGHT = re.compile(r"\b(FR|AZ|U2|W6|VY)\s*[- ]?\s*(\d{2,4})\b", re.I)
AMOUNT = re.compile(r"(?:\bEUR\s*|€\s*)(\d{1,4}[.,]\d{2})\b|\b(\d{1,4}[.,]\d{2})\s*(?:EUR\b|€)", re.I)
NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b")
MONTHS = {
    "GEN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAG": 5, "GIU": 6,
    "LUG": 7, "AGO": 8, "SET": 9, "OTT": 10, "NOV": 11, "DIC": 12,
    "JAN": 1, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "DEC": 12,
}
TEXT_DATE = re.compile(r"\b(\d{1,2})\s+([A-Z]{3})[A-Z]*\s+(\d{4})\b")
ROUTE_SEPARATOR = re.compile(r"\s+(?:A|TO)\s+|\s*[-→]\s*", re.I)
FARE_ITEM = re.compile(r"\b1\s*[xX]\s*(?:TARIFFA|FARE|BIGLIETTO|TICKET)\b", re.I)
ANY_FARE_ITEM = re.compile(r"\b\d+\s*[xX]\s*(?:TARIFFA|FARE|BIGLIETTO|TICKET)\b", re.I)


def _read_image(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    try:
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", "ita+eng", "--psm", "11"],
            input=buffer.getvalue(), capture_output=True, timeout=15, check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise OcrUnavailable("OCR locale non disponibile") from exc
    if result.returncode != 0:
        raise OcrUnavailable("OCR locale non disponibile")
    return result.stdout.decode("utf-8", errors="replace")[:32_000]


def _clean(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value.upper()) if not unicodedata.combining(c))


def _airport_codes(value):
    found = set()
    for code, aliases in AIRPORTS.items():
        if any(re.search(r"\b" + re.escape(alias) + r"\b", value) for alias in aliases):
            found.add(code)
    return found


def _dates(value):
    found = set()
    for day, month, year in NUMERIC_DATE.findall(value):
        try:
            found.add(date(int(year), int(month), int(day)))
        except ValueError:
            pass
    for day, month, year in TEXT_DATE.findall(value):
        if month[:3] in MONTHS:
            try:
                found.add(date(int(year), MONTHS[month[:3]], int(day)))
            except ValueError:
                pass
    return found


def _unique(values):
    return next(iter(values)) if len(values) == 1 else None


def _extract_texts(texts):
    lines_by_doc = {kind: [_clean(line.strip()) for line in text.splitlines() if line.strip()]
                    for kind, text in texts.items()}
    lines = [line for document_lines in lines_by_doc.values() for line in document_lines]
    flights = {f"{prefix}{number}" for line in lines for prefix, number in FLIGHT.findall(line)}
    flight = _unique(flights)
    routes = set()
    for line in lines:
        pieces = ROUTE_SEPARATOR.split(line, maxsplit=1)
        if len(pieces) != 2:
            continue
        origin, destination = _airport_codes(pieces[0]), _airport_codes(pieces[1])
        if len(origin) == len(destination) == 1 and origin != destination:
            routes.add((next(iter(origin)), next(iter(destination))))
    route = _unique(routes)

    flight_dates, purchase_dates = set(), set()
    for kind, document_lines in lines_by_doc.items():
        all_document_dates = set()
        for line in document_lines:
            found = _dates(line)
            all_document_dates.update(found)
            if FLIGHT.search(line) or re.search(r"\b(?:DATA DEL VOLO|PARTENZA|FLIGHT DATE)\b", line):
                flight_dates.update(found)
            if re.search(r"\b(?:ACQUISTO|PAGAMENTO|PURCHASED|ISSUED)\b", line):
                purchase_dates.update(found)
        if kind == "pdf_imbarco" and len(all_document_dates) == 1:
            flight_dates.update(all_document_dates)

    flight_date = _unique(flight_dates)
    purchase_date = _unique(purchase_dates)
    if flight_date and purchase_date and purchase_date > flight_date:
        purchase_date = None

    # The total booking amount can cover multiple passengers or legs. Accept only
    # a single explicitly priced, single-passenger fare item for a single flight.
    fare_prices = []
    receipt_lines = lines_by_doc.get("pdf_ricevuta", [])
    fare_rows = [index for index, line in enumerate(receipt_lines) if FARE_ITEM.search(line)]
    all_fare_rows = [line for line in receipt_lines if ANY_FARE_ITEM.search(line)]
    if flight and len(fare_rows) == len(all_fare_rows) == 1:
        index = fare_rows[0]
        for line in receipt_lines[index:index + 3]:
            amounts = [first or second for first, second in AMOUNT.findall(line)]
            if amounts:
                fare_prices.extend(amounts)
                break
    price = _unique(set(fare_prices))

    return normalize_fields({
        "compagnia_aerea": AIRLINES.get(FLIGHT.match(flight).group(1), "") if flight else "",
        "numero_volo": flight or "",
        "aeroporto_partenza": route[0] if route else "",
        "aeroporto_destinazione": route[1] if route else "",
        "data_acquisto": purchase_date.strftime("%d/%m/%Y") if purchase_date else None,
        "data_volo": flight_date.strftime("%d/%m/%Y") if flight_date else None,
        "costo_tratta": price,
    })


def extract_local(documents):
    """Run offline OCR; never upload documents or log recognized text."""
    texts = {kind: _read_image(document.image) for kind, document in documents.items() if document}
    return _extract_texts(texts)
