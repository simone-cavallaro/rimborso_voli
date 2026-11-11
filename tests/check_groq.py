"""Isolated Groq diagnostic: one synthetic request per case, without retries.

The diagnostic reuses the production client factory and payload builder from
extraction.py, so it sends exactly what the application sends; only the
interactive key input and the fixed output categories are diagnostic-specific.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
import getpass
import json
import logging
import os
import re
import sys
import time
import warnings

import groq
from PIL import Image, ImageDraw, ImageFont

from types import SimpleNamespace

from domain import FIELDS, ValidationError, normalize_fields, parse_extraction
from extraction import (
    DEFAULT_MODEL,
    GROQ_BASE_URL,
    MAX_COMPLETION_TOKENS,
    MAX_RESPONSE_BYTES,
    PROMPT,
    TIMEOUT_SECONDS,
    ConfigurationError,
    InputNotSendable,
    build_client,
    build_payload as _production_build_payload,
)


# The SDK resource path already contains /openai/v1, so the client base URL
# is the documented API domain; the effective request URL is
# https://api.groq.com/openai/v1/chat/completions.
BASE_URL = GROQ_BASE_URL
ENDPOINT = BASE_URL + "/openai/v1/chat/completions"
MODEL = DEFAULT_MODEL
# Bound classification of 403 bodies already received by the SDK.
MAX_ERROR_BODY_BYTES = 16 * 1024
CASES = ("ricevuta", "imbarco", "entrambi", "ambiguo")

CATEGORY_SUCCESS = "successo"
CATEGORY_CONFIG = "configurazione/autorizzazione"
CATEGORY_MODEL = "modello"
CATEGORY_QUOTA = "quota"
CATEGORY_TEMPORARY = "temporaneo"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_NETWORK = "rete"
CATEGORY_INVALID = "risposta non valida"
CATEGORY_REDIRECT = "redirect rifiutato"
CATEGORY_INPUT = "input nascosto non disponibile"
CATEGORY_INTERNAL = "errore interno"
CATEGORY_INTERRUPTED = "interrotto"
CATEGORY_PERMISSION_MODEL_ORG = "permesso modello bloccato (organizzazione)"
CATEGORY_PERMISSION_MODEL_PROJECT = "permesso modello bloccato (progetto)"
CATEGORY_PERMISSION_GROQ = "permessi API Groq"
CATEGORY_CLOUDFLARE_1010 = "blocco accesso Cloudflare 1010"

_KEY_MARKER = re.compile(r"gsk_[A-Za-z0-9_-]+")


class DiagnosticError(Exception):
    """A fixed, non-sensitive diagnostic category."""

    def __init__(self, category, http_status=None):
        super().__init__(category)
        self.category = category
        self.http_status = http_status


@dataclass(frozen=True)
class FieldComparison:
    field: str
    expected: object
    received: object

    @property
    def matches(self):
        return self.expected == self.received


_BASE_VALUES = {
    "compagnia_aerea": "Ryanair",
    "numero_volo": "FR1234",
    "aeroporto_partenza": "FCO",
    "aeroporto_destinazione": "CTA",
    "data_acquisto": "01/11/2026",
    "data_volo": "12/11/2026",
    "costo_tratta": "49.90",
}


def _expected(**overrides):
    values = dict(_BASE_VALUES)
    values.update(overrides)
    return normalize_fields(values)


def _receipt_lines():
    return [
        "DOCUMENTO INVENTATO - TEST",
        "RICEVUTA DI PAGAMENTO - TEST",
        "Compagnia aerea: Ryanair",
        "Numero volo: FR1234",
        "Partenza: FCO",
        "Destinazione: CTA",
        "Data acquisto: 01/11/2026",
        "Data volo: 12/11/2026",
        "Costo tratta: EUR 49.90",
    ]


def _boarding_lines():
    return [
        "DOCUMENTO INVENTATO - TEST",
        "CARTA D'IMBARCO - TEST",
        "Compagnia aerea: Ryanair",
        "Numero volo: FR1234",
        "Partenza: FCO",
        "Destinazione: CTA",
        "Data volo: 12/11/2026",
        "Passeggero: MARIO ROSSI",
    ]


def _ambiguous_lines():
    return [
        "DOCUMENTO INVENTATO - TEST",
        "RICEVUTA DI PRENOTAZIONE - TEST",
        "Compagnia aerea: Ryanair",
        "Numero volo: FR1234",
        "Partenza: FCO",
        "Destinazione: CTA",
        "Data acquisto: 01/11/2026",
        "Data volo: 12/11/2026",
        "Passeggeri: 2 (MARIO ROSSI, LUISA BIANCHI)",
        "Totale prenotazione: EUR 120.00",
        "Costo singolo passeggero: NON INDICATO",
    ]


def case_lines(case):
    """Return the text lines of each synthetic document for a case."""
    if case == "ricevuta":
        return [_receipt_lines()]
    if case == "imbarco":
        return [_boarding_lines()]
    if case == "entrambi":
        return [_receipt_lines(), _boarding_lines()]
    if case == "ambiguo":
        return [_ambiguous_lines()]
    raise ValueError("caso sconosciuto")


def _load_font(size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except (OSError, ValueError):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def _document(lines):
    image = Image.new("RGB", (1600, 1100), "white")
    draw = ImageDraw.Draw(image)
    title_font = _load_font(52)
    body_font = _load_font(38)
    draw.text((48, 40), lines[0], fill="black", font=title_font)
    draw.text((48, 130), lines[1], fill="black", font=title_font)
    y = 240
    for line in lines[2:]:
        draw.text((48, y), line, fill="black", font=body_font)
        y += 64
    return image


def build_case(case):
    """Build the in-memory images and the normalized expected values."""
    documents = case_lines(case)
    images = [_document(lines) for lines in documents]
    if case in ("ricevuta", "entrambi"):
        expected = _expected()
    elif case == "imbarco":
        expected = _expected(data_acquisto=None, costo_tratta=None)
    elif case == "ambiguo":
        expected = _expected(costo_tratta=None)
    else:
        raise ValueError("caso sconosciuto")
    return images, expected


def encode_png(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def build_payload(images):
    """Production payload over the synthetic in-memory images.

    The images are wrapped as the validated documents the application
    prepares, so production JPEG copies, size limits and serialization are
    exercised unchanged.
    """
    documents = {}
    for field, image in zip(("pdf_ricevuta", "pdf_imbarco"), images):
        documents[field] = SimpleNamespace(image=image)
    return _production_build_payload(documents)


def _hidden_input_available():
    if not sys.stdin.isatty():
        return False
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    try:
        os.close(fd)
    except OSError:
        return False
    return True


def _category_for_status(status):
    if 300 <= status <= 399:
        return CATEGORY_REDIRECT
    if status in (400, 401, 403):
        return CATEGORY_CONFIG
    if status == 404:
        return CATEGORY_MODEL
    if status == 429:
        return CATEGORY_QUOTA
    if status == 408 or 500 <= status <= 599:
        return CATEGORY_TEMPORARY
    return CATEGORY_INVALID


def _close_quietly(response):
    try:
        response.close()
    except Exception:
        pass


def _error_body(exc):
    """Bounded, in-memory view of an already received SDK error body.

    The SDK reads error bodies eagerly while raising APIStatusError, so no
    extra download happens here; the cap bounds what reaches the 403
    classifier only. Returns None when no body is available: the caller
    then falls back to the generic 403 category.
    """
    response = getattr(exc, "response", None)
    content = getattr(response, "content", None)
    if not isinstance(content, bytes):
        return None
    return content[: MAX_ERROR_BODY_BYTES + 1]


@contextmanager
def _silent_logging():
    """Silence all logging while the official SDK runs in this standalone CLI.

    The SDK and its HTTP stack log request options, URLs, headers and
    exceptions (with exc_info) at DEBUG, and GROQ_LOG can raise their
    verbosity, so emitted records could carry private values. The guard is
    temporary and always restored in finally: the application and server
    logging are never modified.
    """
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def _default_client_factory(key):
    """Reuse the production SDK client; map the local configuration refusal.

    The production factory fixes base URL, hidden key, timeout, no retries
    and no redirects, and refuses GROQ_CUSTOM_HEADERS before any SDK or
    transport object exists.
    """

    def factory():
        try:
            return build_client(key)
        except ConfigurationError:
            raise DiagnosticError(CATEGORY_CONFIG) from None

    return factory


def _reject_json_constant(_value):
    raise ValueError("Non-standard JSON constant")


def _classify_403_body(raw):
    """Map a 403 body to a fixed diagnostic category, or the generic one.

    Only fully structured, documented signatures are recognized: Groq
    error.type "permissions_error" with its documented codes
    (model_permission_blocked_org / model_permission_blocked_project) and
    the Cloudflare error 1010 (exact whole-string text match, or top-level
    JSON with error_code 1010, error_name "browser_signature_banned" and
    cloudflare_error true). Partial matches inside untrusted messages are
    ignored; unknown, malformed, truncated or unreadable bodies fall back
    to the generic 403 category. This is a diagnostic aid only: it does not
    verify provider authenticity and does not bypass any control, and the
    provider text is never shown or serialized.
    """
    if not isinstance(raw, bytes) or len(raw) > MAX_ERROR_BODY_BYTES:
        return CATEGORY_CONFIG
    try:
        text = raw.decode("utf-8")
        if text.strip() == "error code: 1010":
            return CATEGORY_CLOUDFLARE_1010
        document = json.loads(text, parse_constant=_reject_json_constant)
    except Exception:
        return CATEGORY_CONFIG
    if not isinstance(document, dict):
        return CATEGORY_CONFIG
    if (
        document.get("error_code") == 1010
        and document.get("error_name") == "browser_signature_banned"
        and document.get("cloudflare_error") is True
    ):
        return CATEGORY_CLOUDFLARE_1010
    error = document.get("error")
    if isinstance(error, dict) and error.get("type") == "permissions_error":
        code = error.get("code")
        if code == "model_permission_blocked_org":
            return CATEGORY_PERMISSION_MODEL_ORG
        if code == "model_permission_blocked_project":
            return CATEGORY_PERMISSION_MODEL_PROJECT
        return CATEGORY_PERMISSION_GROQ
    return CATEGORY_CONFIG


def send_request(payload, key, client_factory=None):
    """Send exactly one request via the official SDK; never retry and never
    expose provider text."""
    factory = _default_client_factory(key) if client_factory is None else client_factory
    client = None
    try:
        with _silent_logging():
            try:
                client = factory()
                api_response = client.chat.completions.with_raw_response.create(**payload)
            except groq.APIStatusError as exc:
                # The SDK raised on a 4xx/5xx response and already read its
                # body; only the 403 classification looks at it, capped.
                status = exc.status_code
                if status == 403:
                    category = _classify_403_body(_error_body(exc))
                else:
                    category = _category_for_status(status)
                raise DiagnosticError(category, status) from None
            except groq.APITimeoutError:
                raise DiagnosticError(CATEGORY_TIMEOUT) from None
            except groq.APIConnectionError:
                raise DiagnosticError(CATEGORY_NETWORK) from None
    finally:
        if client is not None:
            with _silent_logging():
                _close_quietly(client)

    status = api_response.status_code
    if status != 200:
        # Defensive: the SDK raises APIStatusError for 4xx/5xx, so this path
        # is unexpected; no body is read here.
        raise DiagnosticError(_category_for_status(status), status)
    try:
        content = api_response.read()
    except Exception:
        # The SDK already received the body eagerly; a failure converting
        # it must not surface provider text.
        raise DiagnosticError(CATEGORY_INVALID, status) from None
    if not isinstance(content, bytes) or len(content) > MAX_RESPONSE_BYTES:
        raise DiagnosticError(CATEGORY_INVALID, status)
    return status, content


def parse_response(raw):
    if not isinstance(raw, bytes) or not raw:
        raise DiagnosticError(CATEGORY_INVALID)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        raise DiagnosticError(CATEGORY_INVALID) from None
    if not isinstance(document, dict):
        raise DiagnosticError(CATEGORY_INVALID)
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise DiagnosticError(CATEGORY_INVALID)
    choice = choices[0]
    if not isinstance(choice, dict):
        raise DiagnosticError(CATEGORY_INVALID)
    if choice.get("finish_reason") == "length":
        raise DiagnosticError(CATEGORY_INVALID)
    message = choice.get("message")
    if not isinstance(message, dict):
        raise DiagnosticError(CATEGORY_INVALID)
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise DiagnosticError(CATEGORY_INVALID)
    try:
        return parse_extraction(content)
    except ValidationError:
        raise DiagnosticError(CATEGORY_INVALID) from None


def compare_fields(expected, received):
    return [
        FieldComparison(field, expected.get(field), received.get(field))
        for field in FIELDS
    ]


def _redact(text, secret):
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return _KEY_MARKER.sub("[REDACTED]", text)


def _safe_value(value, secret):
    if value is None:
        return "null"
    text = _redact(str(value), secret)
    text = "".join(ch if ch.isprintable() else f"\\x{ord(ch):02x}" for ch in text)
    if len(text) > 240:
        text = text[:237] + "..."
    return json.dumps(text, ensure_ascii=True)


def _emit_error(category, elapsed, status, out):
    print("Esito: ERRORE", file=out)
    print(f"Durata: {elapsed:.2f} s", file=out)
    http = status if status is not None else "non disponibile"
    print(f"HTTP: {http} ({category})", file=out)
    print("Confronto campi: non disponibile", file=out)


def _run_case(case, key, client_factory, *, clock, out):
    start = clock()
    status = None
    try:
        images, expected = build_case(case)
        payload = build_payload(images)
        status, raw = send_request(payload, key, client_factory)
        received = parse_response(raw)
    except DiagnosticError as exc:
        http_status = exc.http_status if exc.http_status is not None else status
        _emit_error(exc.category, clock() - start, http_status, out)
        return 2
    except InputNotSendable:
        # Unreachable with the fixed synthetic images; kept as a safe fixed
        # outcome for the production payload size limit.
        _emit_error(CATEGORY_INPUT, clock() - start, None, out)
        return 2
    except KeyboardInterrupt:
        _emit_error(CATEGORY_INTERRUPTED, clock() - start, None, out)
        return 2

    elapsed = clock() - start
    comparisons = compare_fields(expected, received)
    ok = all(item.matches for item in comparisons)
    print("Esito: OK" if ok else "Esito: DIVERSO", file=out)
    print(f"Durata: {elapsed:.2f} s", file=out)
    print(f"HTTP: {status} ({CATEGORY_SUCCESS})", file=out)
    print("Confronto campi:", file=out)
    for item in comparisons:
        if item.matches:
            print(f"  {item.field}: OK ({_safe_value(item.received, key)})", file=out)
        else:
            print(
                f"  {item.field}: DIVERSO atteso={_safe_value(item.expected, key)} "
                f"ricevuto={_safe_value(item.received, key)}",
                file=out,
            )
    return 0 if ok else 1


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m tests.check_groq",
        description=(
            "Diagnostico Groq isolato con documenti sintetici; "
            "una richiesta per caso, nessun retry."
        ),
    )
    parser.add_argument(
        "--case",
        required=True,
        choices=CASES,
        help="caso sintetico da inviare",
    )
    return parser


def main(
    argv=None,
    *,
    key_reader=None,
    client_factory=None,
    hidden_input_available=None,
    clock=None,
):
    out = sys.stdout
    clock = time.monotonic if clock is None else clock
    args = _build_parser().parse_args(argv)

    available = (
        _hidden_input_available
        if hidden_input_available is None
        else hidden_input_available
    )
    if not available():
        _emit_error(CATEGORY_INPUT, 0.0, None, out)
        return 2

    reader = getpass.getpass if key_reader is None else key_reader
    try:
        with warnings.catch_warnings():
            # getpass warns immediately before falling back to echoed input.
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = reader("Chiave API Groq (input nascosto): ")
    except (EOFError, KeyboardInterrupt):
        _emit_error(CATEGORY_INPUT, 0.0, None, out)
        return 2
    except Exception:
        _emit_error(CATEGORY_INPUT, 0.0, None, out)
        return 2

    if not isinstance(key, str):
        _emit_error(CATEGORY_INPUT, 0.0, None, out)
        return 2
    key = key.strip()
    if not key or any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
        _emit_error(CATEGORY_INPUT, 0.0, None, out)
        return 2

    try:
        return _run_case(args.case, key, client_factory, clock=clock, out=out)
    except KeyboardInterrupt:
        _emit_error(CATEGORY_INTERRUPTED, 0.0, None, out)
        return 2
    except Exception:
        _emit_error(CATEGORY_INTERNAL, 0.0, None, out)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
