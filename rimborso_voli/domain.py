"""Validation shared by the UI, AI boundary and persistence layer."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import json
import re

import img2pdf
from PIL import Image, ImageOps, UnidentifiedImageError


TEXT_FIELDS = (
    "compagnia_aerea", "numero_volo", "aeroporto_partenza", "aeroporto_destinazione"
)
DATE_FIELDS = ("data_acquisto", "data_volo")
FIELDS = (*TEXT_FIELDS, *DATE_FIELDS, "costo_tratta")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000


class ValidationError(ValueError):
    """A safe, user-facing validation message."""


def parse_amount(value):
    """Accept plain decimals and explicit IT/EN thousands groups, never NaN/Inf."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValidationError("Inserisci un costo valido, per esempio 123,45.")
    raw = str(value).strip().removeprefix("€").removesuffix("€").strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", raw):
        raw = raw.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d{1,2}", raw):
        raw = raw.replace(",", "")
    elif re.fullmatch(r"\d+(?:[.,]\d{1,2})?", raw):
        raw = raw.replace(",", ".")
    else:
        raise ValidationError("Costo non valido: usa un importo positivo con al massimo due decimali.")
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValidationError("Inserisci un costo valido.") from exc
    if not amount.is_finite() or not Decimal("0") < amount <= Decimal("100000"):
        raise ValidationError("Il costo deve essere maggiore di zero e non superare 100.000 €.")
    return amount.quantize(Decimal("0.01"))


def parse_date(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValidationError("Inserisci le date nel formato gg/mm/aaaa.")
    value = value.strip()
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValidationError("Data non valida: usa il formato gg/mm/aaaa.")


def normalize_fields(values):
    if not isinstance(values, dict):
        raise ValidationError("I dati devono essere un oggetto con i campi della richiesta.")
    result = {}
    for field in TEXT_FIELDS:
        value = values.get(field)
        if value is None:
            value = ""
        if not isinstance(value, str) or len(value) > 160 or any(ord(c) < 32 for c in value):
            raise ValidationError("I campi di testo devono contenere al massimo 160 caratteri senza caratteri di controllo.")
        result[field] = value.strip()
    for field in DATE_FIELDS:
        result[field] = parse_date(values.get(field))
    if result["data_acquisto"] and result["data_volo"]:
        if result["data_acquisto"] > result["data_volo"]:
            raise ValidationError("La data di acquisto non può essere successiva alla data del volo.")
    amount = parse_amount(values.get("costo_tratta"))
    # A decimal string is accepted by PostgreSQL numeric and avoids float rounding.
    result["costo_tratta"] = str(amount) if amount is not None else None
    return result


def parse_extraction(text):
    if not isinstance(text, str) or len(text) > 32_000:
        raise ValidationError("La risposta dell'analisi non è valida. Puoi compilare i dati manualmente.")
    try:
        values = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValidationError("L'analisi non ha restituito dati validi. Riprova o compila manualmente.") from exc
    if not isinstance(values, dict) or set(values) - set(FIELDS):
        raise ValidationError("L'analisi ha restituito campi inattesi. Compila i dati manualmente.")
    # Legacy AI responses used zero for 'not found'; never persist that as a price.
    if not isinstance(values.get("costo_tratta"), bool) and values.get("costo_tratta") in (0, 0.0, "0", "0.00", "0,00"):
        values["costo_tratta"] = None
    return normalize_fields(values)


def display_date(value):
    try:
        normalized = parse_date(value)
        return datetime.strptime(normalized, "%Y-%m-%d").strftime("%d/%m/%Y") if normalized else ""
    except ValidationError:
        return str(value or "")  # Keep legacy invalid values visible and editable.


def display_amount(value):
    try:
        amount = parse_amount(value)
        return str(amount).replace(".", ",") if amount is not None else ""
    except ValidationError:
        return str(value or "")


def plain_label(value):
    """Escape document-derived text in widgets whose labels interpret Markdown."""
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", value)


@dataclass
class Document:
    image: Image.Image
    pdf: bytes
    digest: str


def prepare_document(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError("Ogni immagine deve essere valida e non superare 10 MB.")
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format not in {"JPEG", "PNG"}:
                raise ValidationError("Sono consentite solo immagini JPEG e PNG.")
            if source.width * source.height > MAX_IMAGE_PIXELS or getattr(source, "n_frames", 1) != 1:
                raise ValidationError("Usa un'immagine singola di massimo 12 megapixel.")
            source.load()
            oriented = ImageOps.exif_transpose(source)
            rgba = oriented.convert("RGBA")
            clean = Image.new("RGB", rgba.size, "white")
            clean.paste(rgba, mask=rgba.getchannel("A"))
        # Re-encode pixels to strip metadata and trailing payloads from uploaded files.
        encoded = BytesIO()
        clean.save(encoded, format="JPEG", quality=95)
        pdf = img2pdf.convert(encoded.getvalue())
        if len(pdf) > MAX_PDF_BYTES:
            raise ValidationError("Il documento generato è troppo grande. Riduci la risoluzione dell'immagine.")
        return Document(clean, pdf, sha256(data).hexdigest())
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValidationError("Immagine non leggibile o danneggiata. Carica un file JPEG o PNG valido.") from exc
