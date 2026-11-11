"""Offline tests for the isolated Groq diagnostic (official SDK, in-memory transport)."""

import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import getpass
from io import StringIO
import json
import logging
import unittest
from unittest.mock import Mock, patch

import groq
import httpx
from PIL import Image

from domain import FIELDS
import extraction
from extraction import PROMPT
from tests import check_groq


KEY = "gsk_test_SECRET123"
OTHER_TOKEN = "gsk_other_provider_marker_456"
ENDPOINT = check_groq.ENDPOINT
HOST = "api.groq.com"


def groq_body(values, *, finish_reason="stop"):
    return json.dumps(
        {
            "choices": [
                {
                    "message": {"content": json.dumps(values)},
                    "finish_reason": finish_reason,
                }
            ]
        }
    ).encode("utf-8")


def groq_403_body(code):
    return json.dumps({"error": {"type": "permissions_error", "code": code}}).encode()


CLOUDFLARE_1010_JSON = json.dumps(
    {
        "error_code": 1010,
        "error_name": "browser_signature_banned",
        "cloudflare_error": True,
    }
).encode()


def payload_for(case="ricevuta"):
    return check_groq.build_payload(check_groq.build_case(case)[0])


def mock_client(handler, key=KEY):
    """Official Groq SDK client over httpx.MockTransport: no network at all.

    The handler receives every httpx.Request actually built by the SDK, so
    tests can verify serialization, host, auth and request count. Returns
    (client, calls).
    """
    calls = []

    def tracking(request):
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(tracking)
    client = groq.Groq(
        api_key=key,
        base_url=check_groq.BASE_URL,
        timeout=check_groq.TIMEOUT_SECONDS,
        max_retries=0,
        http_client=httpx.Client(transport=transport, follow_redirects=False),
    )
    return client, calls


def run_main(
    case,
    *,
    key=KEY,
    client=None,
    client_factory=None,
    hidden_input_available=lambda: True,
    key_reader=None,
    clock=None,
):
    out, err = StringIO(), StringIO()
    if client is not None and client_factory is None:
        client_factory = lambda: client
    if key_reader is None:
        key_reader = lambda prompt: key
    if clock is None:
        ticks = iter([0.0, 1.25])
        clock = ticks.__next__
    with redirect_stdout(out), redirect_stderr(err):
        code = check_groq.main(
            ["--case", case],
            key_reader=key_reader,
            client_factory=client_factory,
            hidden_input_available=hidden_input_available,
            clock=clock,
        )
    return code, out.getvalue(), err.getvalue()


class FixtureTests(unittest.TestCase):
    def test_cases_have_synthetic_documents_and_all_fields(self):
        counts = {"ricevuta": 1, "imbarco": 1, "entrambi": 2, "ambiguo": 1}
        for case, count in counts.items():
            documents = check_groq.case_lines(case)
            images, expected = check_groq.build_case(case)
            self.assertEqual(len(documents), count)
            self.assertEqual(len(images), count)
            self.assertEqual(set(expected), set(FIELDS))
            for lines in documents:
                self.assertEqual(lines[0], "DOCUMENTO INVENTATO - TEST")
                self.assertGreaterEqual(len(lines), 2)
            for image in images:
                self.assertIsInstance(image, Image.Image)

    def test_receipt_expected_values_are_normalized(self):
        _, expected = check_groq.build_case("ricevuta")
        self.assertEqual(expected["compagnia_aerea"], "Ryanair")
        self.assertEqual(expected["numero_volo"], "FR1234")
        self.assertEqual(expected["aeroporto_partenza"], "FCO")
        self.assertEqual(expected["aeroporto_destinazione"], "CTA")
        self.assertEqual(expected["data_acquisto"], "2026-11-01")
        self.assertEqual(expected["data_volo"], "2026-11-12")
        self.assertEqual(expected["costo_tratta"], "49.90")

    def test_boarding_has_no_purchase_date_or_price(self):
        _, expected = check_groq.build_case("imbarco")
        self.assertIsNone(expected["data_acquisto"])
        self.assertIsNone(expected["costo_tratta"])
        self.assertEqual(expected["data_volo"], "2026-11-12")

    def test_both_documents_reuse_the_receipt_expected_values(self):
        _, receipt = check_groq.build_case("ricevuta")
        _, both = check_groq.build_case("entrambi")
        self.assertEqual(receipt, both)

    def test_ambiguous_case_does_not_divide_the_total(self):
        _, expected = check_groq.build_case("ambiguo")
        self.assertIsNone(expected["costo_tratta"])
        lines = "\n".join(check_groq.case_lines("ambiguo")[0])
        self.assertIn("Totale prenotazione: EUR 120.00", lines)
        self.assertIn("Costo singolo passeggero: NON INDICATO", lines)
        self.assertNotIn("60.00", lines)

    def test_png_is_generated_in_memory(self):
        images, _ = check_groq.build_case("ricevuta")
        data = check_groq.encode_png(images[0])
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(data)) as decoded:
            self.assertEqual(decoded.format, "PNG")
            self.assertGreater(decoded.width, 0)
            self.assertGreater(decoded.height, 0)


class PayloadTests(unittest.TestCase):
    def test_payload_reuses_the_production_jpeg_copies(self):
        for case in check_groq.CASES:
            images, expected = check_groq.build_case(case)
            payload = check_groq.build_payload(images)
            self.assertEqual(payload["model"], "qwen/qwen3.8-27b")
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertEqual(payload["max_completion_tokens"], 1500)
            content = payload["messages"][0]["content"]
            self.assertEqual(content[0], {"type": "text", "text": PROMPT})
            image_parts = [part for part in content if part["type"] == "image_url"]
            self.assertEqual(len(image_parts), len(images))
            for part, original in zip(image_parts, images):
                url = part["image_url"]["url"]
                self.assertTrue(url.startswith("data:image/jpeg;base64,"))
                raw = base64.b64decode(url.split(",", 1)[1])
                self.assertTrue(raw.startswith(b"\xff\xd8"))
                with Image.open(io.BytesIO(raw)) as copy:
                    self.assertEqual(copy.format, "JPEG")
                    self.assertLessEqual(max(copy.size), 2048)
                # The synthetic source images are never altered.
                self.assertEqual(original.size, (1600, 1100))
            self.assertEqual(
                set(payload),
                {"model", "messages", "response_format", "max_completion_tokens"},
            )
            self.assertNotIn('"expected"', json.dumps(payload))
            for value in expected.values():
                if value is not None:
                    self.assertNotIn(value, content[0]["text"])

    def test_payload_stays_under_the_production_size_limit(self):
        for case in check_groq.CASES:
            payload = check_groq.build_payload(check_groq.build_case(case)[0])
            self.assertLess(
                len(json.dumps(payload, ensure_ascii=True).encode("utf-8")),
                extraction.MAX_PAYLOAD_BYTES,
            )

    def test_there_are_no_more_than_two_images(self):
        for case in check_groq.CASES:
            images, _ = check_groq.build_case(case)
            self.assertLessEqual(len(images), 2)


class DefaultClientTests(unittest.TestCase):
    def test_custom_headers_are_rejected_by_the_production_factory(self):
        for value in ("", f"Authorization: {OTHER_TOKEN}\nUser-Agent: test-client\nHost: other.invalid"):
            with self.subTest(empty=not value):
                factory = check_groq._default_client_factory(KEY)
                with patch.dict("os.environ", {"GROQ_CUSTOM_HEADERS": value}), \
                     patch("extraction.groq.Groq") as constructor, \
                     patch("extraction.groq.DefaultHttpxClient") as http_client:
                    with self.assertRaises(check_groq.DiagnosticError) as caught:
                        factory()
                self.assertEqual(caught.exception.category, check_groq.CATEGORY_CONFIG)
                constructor.assert_not_called()
                http_client.assert_not_called()

    def test_custom_headers_stop_the_production_factory_cli_before_sending(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=json.loads(groq_body({})))

        def http_client(**kwargs):
            return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

        custom = f"Authorization: {OTHER_TOKEN}\nUser-Agent: test-client\nHost: other.invalid"
        with patch.dict("os.environ", {"GROQ_CUSTOM_HEADERS": custom}), \
             patch("extraction.groq.Groq", wraps=groq.Groq) as constructor, \
             patch("extraction.groq.DefaultHttpxClient", side_effect=http_client) as transport:
            code, out, err = run_main("ricevuta")
        self.assertEqual(code, 2)
        self.assertIn("configurazione/autorizzazione", out)
        for marker in (KEY, OTHER_TOKEN, "test-client", "other.invalid", "Traceback"):
            self.assertNotIn(marker, out + err)
        constructor.assert_not_called()
        transport.assert_not_called()
        self.assertEqual(calls, [])

    def test_default_factory_configuration(self):
        factory = check_groq._default_client_factory(KEY)
        client = factory()
        try:
            self.assertEqual(str(client.base_url).rstrip("/"), check_groq.BASE_URL)
            self.assertEqual(client.max_retries, 0)
            self.assertEqual(client.timeout, 45)
            # Redirects are refused on the documented httpx client.
            self.assertFalse(client._client.follow_redirects)
        finally:
            client.close()

    def test_base_url_env_variables_are_ignored(self):
        factory = check_groq._default_client_factory(KEY)
        with patch.dict(
            "os.environ",
            {"GROQ_BASE_URL": "https://other.invalid/v1", "GROQ_API_KEY": OTHER_TOKEN},
        ):
            client = factory()
        try:
            self.assertEqual(str(client.base_url).rstrip("/"), check_groq.BASE_URL)
        finally:
            client.close()

    def test_auth_is_only_the_header_and_target_is_the_fixed_base_url(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            return httpx.Response(200, json=json.loads(groq_body({})))

        client, calls = mock_client(handler)
        with patch.dict(
            "os.environ",
            {"GROQ_API_KEY": OTHER_TOKEN, "GROQ_BASE_URL": "https://other.invalid/v1"},
        ):
            status, _raw = check_groq.send_request(payload_for(), KEY, lambda: client)
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(seen["url"], ENDPOINT)
        self.assertEqual(seen["headers"]["authorization"], f"Bearer {KEY}")
        self.assertNotIn(OTHER_TOKEN, json.dumps(seen["headers"]))


class SdkTransportTests(unittest.TestCase):
    def test_request_serialization_over_mock_transport(self):
        images, _expected = check_groq.build_case("ricevuta")
        payload = check_groq.build_payload(images)
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            seen["body"] = request.content
            return httpx.Response(200, json=json.loads(groq_body({})))

        client, calls = mock_client(handler)
        status, raw = check_groq.send_request(payload, KEY, lambda: client)
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(seen["url"], ENDPOINT)
        self.assertEqual(seen["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(seen["headers"]["content-type"], "application/json")
        body = json.loads(seen["body"])
        self.assertEqual(body, payload)
        self.assertNotIn(KEY.encode(), seen["body"])
        content = body["messages"][0]["content"]
        self.assertEqual(content[0]["text"], PROMPT)
        for part in content[1:]:
            self.assertTrue(part["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_status_errors_are_one_shot_and_bodies_do_not_change_the_category(self):
        cases = {
            400: check_groq.CATEGORY_CONFIG,
            401: check_groq.CATEGORY_CONFIG,
            403: check_groq.CATEGORY_CONFIG,
            404: check_groq.CATEGORY_MODEL,
            429: check_groq.CATEGORY_QUOTA,
            500: check_groq.CATEGORY_TEMPORARY,
            503: check_groq.CATEGORY_TEMPORARY,
        }
        for status, category in cases.items():
            with self.subTest(status=status):
                count = {"n": 0}

                def handler(request, status=status, count=count):
                    count["n"] += 1
                    return httpx.Response(
                        status,
                        json={"error": {"message": f"{KEY} {OTHER_TOKEN}"}},
                    )

                client, calls = mock_client(handler)
                with self.assertRaises(check_groq.DiagnosticError) as caught:
                    check_groq.send_request(payload_for(), KEY, lambda: client)
                self.assertEqual(caught.exception.category, category)
                self.assertEqual(caught.exception.http_status, status)
                self.assertEqual(count["n"], 1)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("gsk_", str(caught.exception))

    def test_timeout_and_connection_errors_are_one_shot(self):
        cases = (
            (httpx.ConnectTimeout, check_groq.CATEGORY_TIMEOUT),
            (httpx.ConnectError, check_groq.CATEGORY_NETWORK),
        )
        for error, category in cases:
            with self.subTest(category=category):
                count = {"n": 0}

                def handler(request, error=error, count=count):
                    count["n"] += 1
                    raise error("simulated transport failure", request=request)

                client, calls = mock_client(handler)
                with self.assertRaises(check_groq.DiagnosticError) as caught:
                    check_groq.send_request(payload_for(), KEY, lambda: client)
                self.assertEqual(caught.exception.category, category)
                self.assertIsNone(caught.exception.http_status)
                self.assertEqual(count["n"], 1)
                self.assertEqual(len(calls), 1)

    def test_redirect_is_refused_without_following(self):
        hosts = []

        def handler(request):
            hosts.append(request.url.host)
            return httpx.Response(
                302, headers={"location": "https://other.invalid/v1/chat/completions"}
            )

        client, calls = mock_client(handler)
        with self.assertRaises(check_groq.DiagnosticError) as caught:
            check_groq.send_request(payload_for(), KEY, lambda: client)
        self.assertEqual(caught.exception.category, check_groq.CATEGORY_REDIRECT)
        self.assertEqual(caught.exception.http_status, 302)
        self.assertEqual(hosts, [HOST])
        self.assertEqual(len(calls), 1)

    def test_success_wrapper_reuses_status_and_existing_parsing(self):
        expected = check_groq.build_case("ricevuta")[1]
        client, calls = mock_client(
            lambda request: httpx.Response(200, json=json.loads(groq_body(expected)))
        )
        status, raw = check_groq.send_request(payload_for(), KEY, lambda: client)
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 1)
        received = check_groq.parse_response(raw)
        self.assertEqual(received, expected)

    def test_oversized_success_body_is_rejected_with_status(self):
        client, calls = mock_client(
            lambda request: httpx.Response(200, content=b"x" * (check_groq.MAX_RESPONSE_BYTES + 1))
        )
        with self.assertRaises(check_groq.DiagnosticError) as caught:
            check_groq.send_request(payload_for(), KEY, lambda: client)
        self.assertEqual(caught.exception.category, check_groq.CATEGORY_INVALID)
        self.assertEqual(caught.exception.http_status, 200)
        self.assertEqual(len(calls), 1)

    def test_handler_exception_becomes_network_error_without_leak(self):
        def handler(request):
            raise RuntimeError(f"{KEY} {OTHER_TOKEN} {ENDPOINT}")

        client, calls = mock_client(handler)
        with self.assertRaises(check_groq.DiagnosticError) as caught:
            check_groq.send_request(payload_for(), KEY, lambda: client)
        self.assertEqual(caught.exception.category, check_groq.CATEGORY_NETWORK)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(KEY, str(caught.exception))


class StatusTests(unittest.TestCase):
    def test_status_categories(self):
        cases = {
            302: check_groq.CATEGORY_REDIRECT,
            400: check_groq.CATEGORY_CONFIG,
            401: check_groq.CATEGORY_CONFIG,
            403: check_groq.CATEGORY_CONFIG,
            404: check_groq.CATEGORY_MODEL,
            408: check_groq.CATEGORY_TEMPORARY,
            418: check_groq.CATEGORY_INVALID,
            429: check_groq.CATEGORY_QUOTA,
            500: check_groq.CATEGORY_TEMPORARY,
            503: check_groq.CATEGORY_TEMPORARY,
        }
        for status, category in cases.items():
            self.assertEqual(check_groq._category_for_status(status), category)


class ResponseParsingTests(unittest.TestCase):
    def test_empty_json_object_is_valid_and_normalized_to_defaults(self):
        received = check_groq.parse_response(groq_body({}))
        self.assertEqual(received["compagnia_aerea"], "")
        self.assertEqual(received["numero_volo"], "")
        self.assertIsNone(received["data_acquisto"])
        self.assertIsNone(received["data_volo"])
        self.assertIsNone(received["costo_tratta"])

    def test_invalid_or_incomplete_responses_are_rejected(self):
        invalid = [
            b"",
            b"\xff",
            b"{",
            b"[]",
            b"{}",
            b'{"choices": []}',
            b'{"choices": "x"}',
            b'{"choices": [{}]}',
            b'{"choices": [{"message": "x"}]}',
            b'{"choices": [{"message": {}}]}',
            b'{"choices": [{"message": {"content": ""}}]}',
            b'{"choices": [{"message": {"content": 1}}]}',
            b'{"choices": [{"message": {"content": "{\"campo_extra\": \"x\"}"}}]}',
            b'{"choices": [{"message": {"content": "{\"costo_tratta\": \"abc\"}"}}]}',
            b'{"choices": [{"message": {"content": "{\"data_volo\": \"31/02/2026\"}"}}]}',
            b'{"choices": [{"message": {"content": "{\"numero_volo\": "}}]}',
            b'{"choices": [{"message": {"content": "{\"numero_volo\": \"AB123\"}"}, "finish_reason": "length"}]}',
        ]
        for raw in invalid:
            with self.assertRaises(check_groq.DiagnosticError) as caught:
                check_groq.parse_response(raw)
            self.assertEqual(caught.exception.category, check_groq.CATEGORY_INVALID)


class Forbidden403ClassificationTests(unittest.TestCase):
    generic = check_groq.CATEGORY_CONFIG

    def groq_403(self, code):
        return json.dumps({"error": {"type": "permissions_error", "code": code}}).encode()

    def test_groq_documented_permission_codes_are_distinguished(self):
        self.assertEqual(
            check_groq._classify_403_body(groq_403_body("model_permission_blocked_org")),
            check_groq.CATEGORY_PERMISSION_MODEL_ORG,
        )
        self.assertEqual(
            check_groq._classify_403_body(
                groq_403_body("model_permission_blocked_project")
            ),
            check_groq.CATEGORY_PERMISSION_MODEL_PROJECT,
        )

    def test_permissions_error_with_unknown_or_missing_code_is_api_permissions(self):
        for body in (
            self.groq_403("some_other_code"),
            self.groq_403(None),
            json.dumps({"error": {"type": "permissions_error"}}).encode(),
            json.dumps({"error": {"type": "permissions_error", "code": 123}}).encode(),
            json.dumps({"error": {"type": "permissions_error", "message": KEY}}).encode(),
        ):
            self.assertEqual(
                check_groq._classify_403_body(body),
                check_groq.CATEGORY_PERMISSION_GROQ,
            )

    def test_non_standard_json_constants_prevent_specific_classification(self):
        for body in (
            b'{"error": {"type": "permissions_error", "code": "model_permission_blocked_org", "message": NaN}}',
            b'{"error_code": 1010, "error_name": "browser_signature_banned", "cloudflare_error": NaN}',
        ):
            self.assertEqual(check_groq._classify_403_body(body), self.generic)

    def test_model_permission_codes_require_the_documented_type(self):
        for body in (
            json.dumps(
                {"error": {"type": "other_error", "code": "model_permission_blocked_org"}}
            ).encode(),
            json.dumps(
                {"error": {"code": "model_permission_blocked_project"}}
            ).encode(),
        ):
            self.assertEqual(check_groq._classify_403_body(body), self.generic)

    def test_non_dict_error_object_is_generic(self):
        for body in (
            json.dumps({"error": "permissions_error"}).encode(),
            json.dumps({"error": ["permissions_error"]}).encode(),
            json.dumps({"error": None}).encode(),
        ):
            self.assertEqual(check_groq._classify_403_body(body), self.generic)

    def test_cloudflare_text_signature_requires_the_exact_whole_string(self):
        self.assertEqual(
            check_groq._classify_403_body(b"error code: 1010"),
            check_groq.CATEGORY_CLOUDFLARE_1010,
        )
        self.assertEqual(
            check_groq._classify_403_body(b"  error code: 1010 \t\n"),
            check_groq.CATEGORY_CLOUDFLARE_1010,
        )

    def test_cloudflare_text_substrings_are_not_matched(self):
        for body in (
            b"error code: 10101",
            b"<html>error code: 1010</html>",
            b"error code: 1010 please contact support",
            b"some message mentioning error code: 1010",
        ):
            self.assertEqual(check_groq._classify_403_body(body), self.generic)

    def test_cloudflare_json_signature_requires_all_documented_fields(self):
        self.assertEqual(
            check_groq._classify_403_body(CLOUDFLARE_1010_JSON),
            check_groq.CATEGORY_CLOUDFLARE_1010,
        )
        partial = [
            json.dumps({"error_code": 1010, "error_name": "browser_signature_banned"}).encode(),
            json.dumps({"error_code": 1010, "cloudflare_error": True}).encode(),
            json.dumps(
                {"error_name": "browser_signature_banned", "cloudflare_error": True}
            ).encode(),
            json.dumps({"error_code": "1010", "error_name": "browser_signature_banned", "cloudflare_error": True}).encode(),
            json.dumps({"error_code": 1010, "error_name": "other_name", "cloudflare_error": True}).encode(),
            json.dumps({"error_code": 4038, "error_name": "browser_signature_banned", "cloudflare_error": True}).encode(),
            json.dumps({"error_code": 1010, "error_name": "browser_signature_banned", "cloudflare_error": False}).encode(),
            json.dumps({"error_code": 1010, "error_name": "browser_signature_banned", "cloudflare_error": "yes"}).encode(),
        ]
        for body in partial:
            self.assertEqual(check_groq._classify_403_body(body), self.generic)

    def test_unexpected_or_unreadable_bodies_fall_back_to_generic(self):
        for body in (
            b"",
            b"\xff",
            b"{",
            b"[1]",
            b'"plain string"',
            b"123",
            b"<html>access denied</html>",
            json.dumps({"message": KEY}).encode(),
            json.dumps({"error": {"type": "invalid_request_error", "message": KEY}}).encode(),
        ):
            self.assertEqual(check_groq._classify_403_body(body), self.generic)

    def test_oversized_or_missing_body_falls_back_to_generic(self):
        self.assertEqual(check_groq._classify_403_body(None), self.generic)
        self.assertEqual(
            check_groq._classify_403_body(b"x" * (check_groq.MAX_ERROR_BODY_BYTES + 1)),
            self.generic,
        )
        self.assertEqual(
            check_groq._classify_403_body(b"x" * check_groq.MAX_ERROR_BODY_BYTES),
            self.generic,
        )

    def test_words_inside_untrusted_messages_do_not_diagnose(self):
        body = json.dumps(
            {
                "error": {
                    "type": "invalid_request_error",
                    "message": "model_permission_blocked_org and error code: 1010",
                }
            }
        ).encode()
        self.assertEqual(check_groq._classify_403_body(body), self.generic)

    def test_category_words_inside_message_do_not_override_unknown_code(self):
        body = json.dumps(
            {
                "error": {
                    "type": "permissions_error",
                    "code": "not_documented",
                    "message": "model_permission_blocked_project",
                }
            }
        ).encode()
        self.assertEqual(
            check_groq._classify_403_body(body),
            check_groq.CATEGORY_PERMISSION_GROQ,
        )


class Forbidden403SendRequestTests(unittest.TestCase):
    def test_api_status_error_403_is_classified_and_one_shot(self):
        cases = (
            (groq_403_body("model_permission_blocked_org"), check_groq.CATEGORY_PERMISSION_MODEL_ORG),
            (groq_403_body("model_permission_blocked_project"), check_groq.CATEGORY_PERMISSION_MODEL_PROJECT),
            (json.dumps({"error": {"type": "permissions_error"}}).encode(), check_groq.CATEGORY_PERMISSION_GROQ),
            (CLOUDFLARE_1010_JSON, check_groq.CATEGORY_CLOUDFLARE_1010),
            (b"error code: 1010", check_groq.CATEGORY_CLOUDFLARE_1010),
            (b"<html>access denied</html>", check_groq.CATEGORY_CONFIG),
        )
        for body, category in cases:
            with self.subTest(category=category):

                def handler(request, body=body):
                    return httpx.Response(403, content=body)

                client, calls = mock_client(handler)
                with self.assertRaises(check_groq.DiagnosticError) as caught:
                    check_groq.send_request(payload_for(), KEY, lambda: client)
                self.assertEqual(caught.exception.category, category)
                self.assertEqual(caught.exception.http_status, 403)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("gsk_", str(caught.exception))

    def test_error_body_reaches_classifier_capped_at_max_plus_one(self):
        class StubResponse:
            content = b"x" * (check_groq.MAX_ERROR_BODY_BYTES + 5)

        class StubError:
            response = StubResponse()

        sliced = check_groq._error_body(StubError())
        self.assertEqual(len(sliced), check_groq.MAX_ERROR_BODY_BYTES + 1)
        self.assertIsNone(check_groq._error_body(object()))

        big = b"x" * (check_groq.MAX_ERROR_BODY_BYTES + 5)
        client, calls = mock_client(lambda request: httpx.Response(403, content=big))
        with self.assertRaises(check_groq.DiagnosticError) as caught:
            check_groq.send_request(payload_for(), KEY, lambda: client)
        self.assertEqual(caught.exception.category, check_groq.CATEGORY_CONFIG)
        self.assertEqual(caught.exception.http_status, 403)
        self.assertEqual(len(calls), 1)

    def test_exact_max_403_body_is_still_classified(self):
        org = json.dumps({"error": {"type": "permissions_error", "code": "model_permission_blocked_org"}})
        pad = (check_groq.MAX_ERROR_BODY_BYTES - len(org)) * " "
        padded = (org + pad).encode()
        self.assertEqual(len(padded), check_groq.MAX_ERROR_BODY_BYTES)
        client, calls = mock_client(lambda request: httpx.Response(403, content=padded))
        with self.assertRaises(check_groq.DiagnosticError) as caught:
            check_groq.send_request(payload_for(), KEY, lambda: client)
        self.assertEqual(caught.exception.category, check_groq.CATEGORY_PERMISSION_MODEL_ORG)
        self.assertEqual(len(calls), 1)

    def test_oversized_valid_json_with_padding_is_not_classified(self):
        for signature in (groq_403_body("model_permission_blocked_org"), CLOUDFLARE_1010_JSON):
            for size in (check_groq.MAX_ERROR_BODY_BYTES + 1, check_groq.MAX_ERROR_BODY_BYTES + 200):
                with self.subTest(size=size, signature=signature):
                    body = signature + b" " * (size - len(signature))
                    client, calls = mock_client(lambda request: httpx.Response(403, content=body))
                    code, out, err = run_main("ricevuta", client=client)
                    self.assertEqual(code, 2)
                    self.assertIn("HTTP: 403 (configurazione/autorizzazione)", out)
                    self.assertNotIn(KEY, out + err)
                    self.assertEqual(len(calls), 1)

    def test_403_without_body_or_with_missing_response_is_generic(self):
        client, calls = mock_client(lambda request: httpx.Response(403))
        with self.assertRaises(check_groq.DiagnosticError) as caught:
            check_groq.send_request(payload_for(), KEY, lambda: client)
        self.assertEqual(caught.exception.category, check_groq.CATEGORY_CONFIG)
        self.assertEqual(caught.exception.http_status, 403)
        self.assertEqual(len(calls), 1)

        class Empty:
            pass

        self.assertIsNone(check_groq._error_body(Empty()))
        self.assertEqual(check_groq._classify_403_body(None), check_groq.CATEGORY_CONFIG)


class Forbidden403FlowTests(unittest.TestCase):
    def test_api_status_error_403_subcategories_in_cli_flow(self):
        cases = (
            (groq_403_body("model_permission_blocked_org"), check_groq.CATEGORY_PERMISSION_MODEL_ORG),
            (groq_403_body("model_permission_blocked_project"), check_groq.CATEGORY_PERMISSION_MODEL_PROJECT),
            (json.dumps({"error": {"type": "permissions_error"}}).encode(), check_groq.CATEGORY_PERMISSION_GROQ),
            (CLOUDFLARE_1010_JSON, check_groq.CATEGORY_CLOUDFLARE_1010),
            (b"error code: 1010", check_groq.CATEGORY_CLOUDFLARE_1010),
        )
        for body, category in cases:
            with self.subTest(category=category):

                def handler(request, body=body):
                    return httpx.Response(403, content=body)

                client, calls = mock_client(handler)
                code, out, err = run_main("ricevuta", client_factory=lambda: client)
                self.assertEqual(code, 2)
                self.assertIn(f"HTTP: 403 ({category})", out)
                self.assertIn("Confronto campi: non disponibile", out)
                self.assertNotIn(KEY, out + err)
                self.assertNotIn("gsk_", out + err)
                self.assertNotIn(ENDPOINT, out + err)
                self.assertNotIn("Traceback", out + err)
                self.assertEqual(err, "")
                self.assertEqual(len(calls), 1)

    def test_api_status_error_403_generic_fallbacks_in_cli_flow(self):
        cases = (
            b"<html>error code: 1010</html>",
            b"{",
            json.dumps(
                {"error": {"message": "contains model_permission_blocked_org inside"}}
            ).encode(),
        )
        for body in cases:
            with self.subTest(body=body[:24]):

                def handler(request, body=body):
                    return httpx.Response(403, content=body)

                client, calls = mock_client(handler)
                code, out, err = run_main("ricevuta", client_factory=lambda: client)
                self.assertEqual(code, 2)
                self.assertIn("HTTP: 403 (configurazione/autorizzazione)", out)
                for marker in (KEY, "gsk_", ENDPOINT, "model_permission_blocked_org"):
                    self.assertNotIn(marker, out + err)
                self.assertNotIn("Traceback", out + err)
                self.assertEqual(len(calls), 1)


class SdkLoggingGuardTests(unittest.TestCase):
    def test_same_logger_levels_work_again_after_guard(self):
        probe = logging.getLogger("tests.groq_same_logger_cache")
        previous_level = probe.level
        previous_disable = logging.root.manager.disable
        try:
            probe.setLevel(logging.DEBUG)
            logging.disable(logging.NOTSET)
            self.assertTrue(probe.isEnabledFor(logging.DEBUG))
            with check_groq._silent_logging():
                self.assertFalse(probe.isEnabledFor(logging.DEBUG))
            self.assertTrue(probe.isEnabledFor(logging.DEBUG))
        finally:
            probe.setLevel(previous_level)
            logging.disable(previous_disable)

    def _capture_targets(self):
        return [
            logging.getLogger(),
            logging.getLogger("groq"),
            logging.getLogger("groq._base_client"),
            logging.getLogger("groq.resources.chat.completions"),
            logging.getLogger("httpx"),
            logging.getLogger("httpcore"),
        ]

    def test_no_sdk_log_leak_and_full_restoration(self):
        marker = "private-provider-body-MARKER-SDK-77"

        def handler(request):
            return httpx.Response(
                403,
                json={
                    "error": {
                        "type": "permissions_error",
                        "code": "model_permission_blocked_org",
                        "message": marker,
                    }
                },
            )

        client, calls = mock_client(handler)
        emitted = []

        class Capture(logging.Handler):
            def emit(self, record):
                try:
                    emitted.append(f"[{record.name}] {record.getMessage()} {record.exc_text}")
                except Exception as exc:
                    emitted.append(f"[format-error] {record.name}: {exc}")

        targets = self._capture_targets()
        old_levels = [(t, t.level) for t in targets]
        handlers = []
        previous_disable = logging.root.manager.disable
        try:
            for target in targets:
                target.setLevel(logging.DEBUG)
                capture = Capture()
                target.addHandler(capture)
                handlers.append(capture)
            with patch.dict("os.environ", {"GROQ_LOG": "debug"}):
                code, out, err = run_main("ricevuta", client_factory=lambda: client)
            self.assertEqual(code, 2)
            self.assertIn(f"HTTP: 403 ({check_groq.CATEGORY_PERMISSION_MODEL_ORG})", out)
            joined = "\n".join(emitted) + out + err
            self.assertNotIn(marker, joined)
            self.assertNotIn(KEY, joined)
            # The guard is fully restored after the run.
            self.assertEqual(logging.root.manager.disable, previous_disable)
        finally:
            for (target, level), capture in zip(old_levels, handlers):
                target.setLevel(level)
                target.removeHandler(capture)
            logging.root.manager.disable = previous_disable
        # After the guard, ordinary logging flows again.
        with self.assertLogs("tests.groq_guard_probe", level="INFO") as logs:
            logging.getLogger("tests.groq_guard_probe").info("safe record after guard")
        self.assertIn("safe record after guard", " ".join(logs.output))

    def test_guard_is_restored_on_interrupt(self):
        def handler(request):
            raise KeyboardInterrupt()

        client, calls = mock_client(handler)
        previous_disable = logging.root.manager.disable
        try:
            code, out, err = run_main("ricevuta", client_factory=lambda: client)
            self.assertEqual(code, 2)
            self.assertIn("interrotto", out)
            self.assertNotIn("Traceback", out + err)
            self.assertNotIn(KEY, out + err)
            self.assertEqual(len(calls), 1)
            self.assertEqual(logging.root.manager.disable, previous_disable)
        finally:
            logging.root.manager.disable = previous_disable

    def test_guard_is_restored_on_connection_error(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        client, calls = mock_client(handler)
        previous_disable = logging.root.manager.disable
        try:
            code, out, err = run_main("ricevuta", client_factory=lambda: client)
            self.assertEqual(code, 2)
            self.assertIn("HTTP: non disponibile (rete)", out)
            self.assertNotIn("Traceback", out + err)
            self.assertNotIn(KEY, out + err)
            self.assertEqual(len(calls), 1)
            self.assertEqual(logging.root.manager.disable, previous_disable)
        finally:
            logging.root.manager.disable = previous_disable


class ComparisonTests(unittest.TestCase):
    def test_compare_fields_covers_every_field(self):
        expected = check_groq.build_case("ricevuta")[1]
        comparisons = check_groq.compare_fields(expected, dict(expected))
        self.assertEqual([item.field for item in comparisons], list(FIELDS))
        self.assertTrue(all(item.matches for item in comparisons))

    def test_compare_fields_reports_a_different_value(self):
        expected = check_groq.build_case("ricevuta")[1]
        received = dict(expected)
        received["costo_tratta"] = "60.00"
        comparisons = check_groq.compare_fields(expected, received)
        self.assertEqual(
            [item.field for item in comparisons if not item.matches],
            ["costo_tratta"],
        )

    def test_safe_value_redacts_keys_and_control_characters(self):
        self.assertEqual(check_groq._safe_value(None, KEY), "null")
        redacted = check_groq._safe_value(f"{KEY} / {OTHER_TOKEN}", KEY)
        self.assertIn("[REDACTED]", redacted)
        self.assertNotIn(KEY, redacted)
        self.assertNotIn("gsk_", redacted)
        escaped = check_groq._safe_value("a\nb", KEY)
        self.assertNotIn("\n", escaped)
        self.assertIn("\\x0a", escaped)
        long_value = check_groq._safe_value("x" * 300, KEY)
        self.assertLessEqual(len(long_value), 245)


class MainTests(unittest.TestCase):
    def test_help_does_not_read_key_or_open_network(self):
        out, err = StringIO(), StringIO()
        with patch("tests.check_groq.getpass.getpass") as getpass_mock, patch(
            "tests.check_groq._default_client_factory"
        ) as factory_mock:
            with redirect_stdout(out), redirect_stderr(err):
                with self.assertRaises(SystemExit) as caught:
                    check_groq.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        getpass_mock.assert_not_called()
        factory_mock.assert_not_called()
        self.assertIn("--case", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_missing_case_exits_without_reading_key(self):
        with patch("tests.check_groq.getpass.getpass") as getpass_mock:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    check_groq.main([])
        self.assertEqual(caught.exception.code, 2)
        getpass_mock.assert_not_called()

    def test_non_tty_stops_before_reading_the_key(self):
        key_reader = Mock()
        client, calls = mock_client(
            lambda request: httpx.Response(200, json=json.loads(groq_body({})))
        )
        code, out, err = run_main(
            "ricevuta",
            key_reader=key_reader,
            hidden_input_available=lambda: False,
            client_factory=lambda: client,
        )
        self.assertEqual(code, 2)
        self.assertIn("input nascosto non disponibile", out)
        self.assertEqual(len(calls), 0)
        key_reader.assert_not_called()
        self.assertEqual(err, "")

    def test_eof_and_keyboard_interrupt_are_handled_without_traceback(self):
        for exc_type in (EOFError, KeyboardInterrupt):
            def reader(prompt, exc_type=exc_type):
                raise exc_type()

            code, out, err = run_main("ricevuta", key_reader=reader)
            self.assertEqual(code, 2)
            self.assertIn("input nascosto non disponibile", out)
            self.assertNotIn("Traceback", out)
            self.assertEqual(err, "")

    def test_getpass_fallback_stops_before_reading_or_echoing_key(self):
        # A tty may exist while disabling echo still fails. Exercise the real
        # stdlib fallback: its warning must stop execution before _raw_input.
        with patch("getpass._raw_input", return_value=KEY) as raw_input:
            client, calls = mock_client(
                lambda request: httpx.Response(200, json=json.loads(groq_body({})))
            )
            code, out, err = run_main(
                "ricevuta",
                key_reader=getpass.fallback_getpass,
                client_factory=lambda: client,
            )
        raw_input.assert_not_called()
        self.assertEqual(code, 2)
        self.assertIn("input nascosto non disponibile", out)
        self.assertNotIn(KEY, out + err)
        self.assertEqual(err, "")
        self.assertEqual(len(calls), 0)

    def test_interrupt_during_request_does_not_print_traceback(self):
        def handler(request):
            raise KeyboardInterrupt()

        client, calls = mock_client(handler)
        code, out, err = run_main("ricevuta", client_factory=lambda: client)
        self.assertEqual(code, 2)
        self.assertIn("interrotto", out)
        self.assertNotIn(KEY, out + err)
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(len(calls), 1)

    def test_empty_or_unsafe_key_is_rejected_without_leaking(self):
        for bad in ("", "   ", f"{KEY} {KEY}", f"{KEY}\nInjected: 1", f"{KEY}\x00"):
            code, out, err = run_main("ricevuta", key=bad, client_factory=lambda: None)
            self.assertEqual(code, 2)
            self.assertIn("input nascosto non disponibile", out)
            self.assertNotIn(KEY, out)
            self.assertEqual(err, "")

    def test_success_exit_is_zero_and_uses_one_request(self):
        expected = check_groq.build_case("ricevuta")[1]
        client, calls = mock_client(
            lambda request: httpx.Response(200, json=json.loads(groq_body(expected)))
        )
        code, out, err = run_main("ricevuta", client_factory=lambda: client)
        self.assertEqual(code, 0)
        self.assertIn("Esito: OK", out)
        self.assertIn("Durata: 1.25 s", out)
        self.assertIn("HTTP: 200 (successo)", out)
        self.assertIn("Confronto campi:", out)
        self.assertIn("compagnia_aerea: OK", out)
        self.assertIn("costo_tratta: OK", out)
        self.assertNotIn(KEY, out)
        self.assertNotIn(ENDPOINT, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(err, "")
        self.assertEqual(len(calls), 1)
        body = json.loads(calls[0].content)
        self.assertEqual(body["model"], "qwen/qwen3.8-27b")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["max_completion_tokens"], 1500)
        self.assertEqual(body["messages"][0]["content"][0]["text"], PROMPT)

    def test_both_case_uses_one_request_with_two_images(self):
        expected = check_groq.build_case("entrambi")[1]
        client, calls = mock_client(
            lambda request: httpx.Response(200, json=json.loads(groq_body(expected)))
        )
        code, out, err = run_main("entrambi", client_factory=lambda: client)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        body = json.loads(calls[0].content)
        image_parts = [
            part
            for part in body["messages"][0]["content"]
            if part["type"] == "image_url"
        ]
        self.assertEqual(len(image_parts), 2)
        self.assertEqual(err, "")

    def test_mismatch_exit_is_one(self):
        expected = check_groq.build_case("ricevuta")[1]
        values = dict(expected)
        values["costo_tratta"] = "60.00"
        client, calls = mock_client(
            lambda request: httpx.Response(200, json=json.loads(groq_body(values)))
        )
        code, out, err = run_main("ricevuta", client_factory=lambda: client)
        self.assertEqual(code, 1)
        self.assertIn("Esito: DIVERSO", out)
        self.assertIn("costo_tratta: DIVERSO", out)
        self.assertIn('atteso="49.90"', out)
        self.assertIn('ricevuto="60.00"', out)
        self.assertNotIn(KEY, out)
        self.assertEqual(err, "")

    def test_invalid_response_keeps_the_known_http_status(self):
        client, calls = mock_client(
            lambda request: httpx.Response(200, content=b"provider-private-marker " + KEY.encode())
        )
        code, out, err = run_main("ricevuta", client_factory=lambda: client)
        self.assertEqual(code, 2)
        self.assertIn("HTTP: 200 (risposta non valida)", out)
        self.assertNotIn(KEY, out + err)
        self.assertNotIn("provider-private-marker", out + err)
        self.assertEqual(len(calls), 1)

    def test_http_error_categories_are_fixed_and_one_shot(self):
        cases = {
            400: check_groq.CATEGORY_CONFIG,
            401: check_groq.CATEGORY_CONFIG,
            403: check_groq.CATEGORY_CONFIG,
            404: check_groq.CATEGORY_MODEL,
            408: check_groq.CATEGORY_TEMPORARY,
            429: check_groq.CATEGORY_QUOTA,
            500: check_groq.CATEGORY_TEMPORARY,
            503: check_groq.CATEGORY_TEMPORARY,
        }
        for status, category in cases.items():
            def handler(request, status=status):
                return httpx.Response(status, json={"error": {"message": f"{KEY} {OTHER_TOKEN}"}})

            client, calls = mock_client(handler)
            code, out, err = run_main("ricevuta", client_factory=lambda: client)
            self.assertEqual(code, 2)
            self.assertIn(f"HTTP: {status} ({category})", out)
            self.assertIn("Confronto campi: non disponibile", out)
            self.assertNotIn(KEY, out)
            self.assertNotIn("gsk_", out)
            self.assertNotIn(ENDPOINT, out)
            self.assertNotIn("Traceback", out)
            self.assertEqual(len(calls), 1)
            self.assertEqual(err, "")

    def test_timeout_and_network_categories_are_fixed_and_one_shot(self):
        cases = (
            (httpx.ConnectTimeout, check_groq.CATEGORY_TIMEOUT),
            (httpx.ConnectError, check_groq.CATEGORY_NETWORK),
        )
        for error, category in cases:
            def handler(request, error=error):
                raise error("simulated transport failure", request=request)

            client, calls = mock_client(handler)
            code, out, err = run_main("ricevuta", client_factory=lambda: client)
            self.assertEqual(code, 2)
            self.assertIn(f"HTTP: non disponibile ({category})", out)
            self.assertNotIn(KEY, out)
            self.assertNotIn("gsk_", out)
            self.assertNotIn("Traceback", out)
            self.assertEqual(len(calls), 1)
            self.assertEqual(err, "")

    def test_reflected_key_is_redacted_in_success_fields(self):
        expected = check_groq.build_case("ricevuta")[1]
        values = dict(expected)
        values["compagnia_aerea"] = f"{KEY} {OTHER_TOKEN}"
        client, calls = mock_client(
            lambda request: httpx.Response(200, json=json.loads(groq_body(values)))
        )
        code, out, err = run_main("ricevuta", client_factory=lambda: client)
        self.assertEqual(code, 1)
        self.assertIn("compagnia_aerea: DIVERSO", out)
        self.assertIn("[REDACTED]", out)
        self.assertNotIn(KEY, out)
        self.assertNotIn("gsk_", out)
        self.assertEqual(err, "")

    def test_unexpected_failure_does_not_leak_or_print_traceback(self):
        def boom_factory():
            raise RuntimeError(f"{KEY} {OTHER_TOKEN} {ENDPOINT}")

        code, out, err = run_main("ricevuta", client_factory=boom_factory)
        self.assertEqual(code, 2)
        self.assertIn("HTTP: non disponibile (errore interno)", out)
        self.assertNotIn(KEY, out)
        self.assertNotIn("gsk_", out)
        self.assertNotIn(ENDPOINT, out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
