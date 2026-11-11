"""Offline tests for the production Groq extraction (real SDK, mock transport)."""

import base64
from io import BytesIO
import io
import json
import logging
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import groq
import httpx
from PIL import Image

import extraction
from domain import ValidationError
from security import log_failure
from extraction import (
    DEFAULT_MODEL,
    PROMPT,
    ConfigurationError,
    InputNotSendable,
    _GroqDependencyFilter,
    _groq_request_scope,
    build_client,
    build_payload,
    parse_completion,
    restrict_dependency_logs,
)


ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
HOST = "api.groq.com"
KEY = "test-key"
DEPENDENCY_LOGGERS = (
    "groq", "groq._base_client", "groq._response",
    "httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
    "httpcore.proxy", "httpcore.socks",
)


def document(size=(8, 8), color="white"):
    return SimpleNamespace(image=Image.new("RGB", size, color))


def valid_content(**fields):
    payload = {"numero_volo": "AB123"}
    payload.update(fields)
    return json.dumps(payload)


def success_response(**fields):
    return {"choices": [{"message": {"content": valid_content(**fields)},
                         "finish_reason": "stop"}]}


def tracking(handler, calls):
    def tracked(request):
        calls.append(request)
        return handler(request)
    return tracked


def run_extract(handler, documents=None, **kwargs):
    """Run the production factory over an in-memory transport.

    The patched http client class records the arguments production passes to
    the transport, so the tests verify what the real factory requests.
    """
    if documents is None:
        documents = {"pdf_ricevuta": document()}
    calls, seen = [], {}

    def http_client(**kwargs2):
        seen.update(kwargs2)
        return httpx.Client(transport=httpx.MockTransport(tracking(handler, calls)), **kwargs2)

    with patch("extraction.groq.DefaultHttpxClient", side_effect=http_client):
        values = extraction.extract(documents, KEY, **kwargs)
    return values, calls, seen


class TransportTests(unittest.TestCase):
    def test_request_targets_the_documented_groq_endpoint(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            return httpx.Response(200, json=success_response(costo_tratta="45.99"))

        values, calls, transport_kwargs = run_extract(handler)
        self.assertEqual(values["numero_volo"], "AB123")
        self.assertEqual(values["costo_tratta"], "45.99")
        self.assertEqual(len(calls), 1)
        self.assertEqual(seen["url"], ENDPOINT)
        self.assertEqual(seen["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(seen["headers"]["content-type"], "application/json")
        self.assertNotIn(KEY.encode(), calls[0].content)
        # The production factory passes the documented one-shot transport.
        self.assertIs(transport_kwargs.get("follow_redirects"), False)

    def test_payload_serializes_prompt_json_mode_and_jpeg_copies(self):
        requests = []
        original = Image.new("RGB", (3000, 2000), "red")

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=success_response())

        documents = {"pdf_ricevuta": SimpleNamespace(image=original),
                     "pdf_imbarco": document((640, 480), "blue")}
        values, calls, _seen = run_extract(handler, documents)
        self.assertEqual(len(calls), 1)
        body = json.loads(calls[0].content)
        self.assertEqual(body["model"], DEFAULT_MODEL)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["max_completion_tokens"], 1500)
        content = body["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": PROMPT})
        self.assertNotIn('"expected"', json.dumps(body))
        parts = [part for part in content if part["type"] == "image_url"]
        self.assertEqual(len(parts), 2)
        for part, source in zip(parts, (original, documents["pdf_imbarco"].image)):
            url = part["image_url"]["url"]
            self.assertTrue(url.startswith("data:image/jpeg;base64,"))
            raw = base64.b64decode(url.split(",", 1)[1])
            self.assertTrue(raw.startswith(b"\xff\xd8"))
            with Image.open(io.BytesIO(raw)) as copy:
                self.assertEqual(copy.format, "JPEG")
                self.assertLessEqual(max(copy.size), extraction.MAX_IMAGE_SIDE)
        # The archived document is never altered.
        self.assertEqual(original.size, (3000, 2000))
        self.assertEqual(original.getpixel((0, 0)), (255, 0, 0))

    def test_payload_uses_at_most_two_images(self):
        payload = build_payload({
            "pdf_ricevuta": document(), "pdf_imbarco": document(),
            "pdf_altro": document(),
        })
        parts = [part for part in payload["messages"][0]["content"]
                 if part["type"] == "image_url"]
        self.assertEqual(len(parts), 2)

    def test_payload_without_documents_keeps_only_the_prompt(self):
        payload = build_payload(None)
        content = payload["messages"][0]["content"]
        self.assertEqual(content, [{"type": "text", "text": PROMPT}])

    def test_non_string_or_empty_model_falls_back_to_default(self):
        for bad in (None, 123, "   "):
            with self.subTest(bad=bad):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(200, json=success_response())

                run_extract(handler, model_name=bad)
                self.assertEqual(json.loads(requests[0].content)["model"], DEFAULT_MODEL)

    def test_custom_model_name_is_used(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=success_response())

        run_extract(handler, model_name=" models/demo ")
        self.assertEqual(json.loads(requests[0].content)["model"], "models/demo")

    def test_typical_payloads_stay_under_the_documented_limit(self):
        payload = build_payload({"pdf_ricevuta": document((3000, 2000)),
                                 "pdf_imbarco": document((3000, 2000))})
        self.assertLess(
            len(json.dumps(payload, ensure_ascii=True).encode("utf-8")),
            extraction.MAX_PAYLOAD_BYTES,
        )

    def test_oversized_payload_is_refused_without_any_call_or_client(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=success_response())

        def http_client(**kwargs):
            calls.append("client")
            return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

        with patch("extraction.MAX_PAYLOAD_BYTES", 1000), \
                patch("extraction.groq.Groq", wraps=groq.Groq) as groq_constructor, \
                patch("extraction.groq.DefaultHttpxClient", side_effect=http_client):
            with self.assertRaises(InputNotSendable):
                extraction.extract({"pdf_ricevuta": document((64, 64))}, KEY)
        self.assertEqual(calls, [])
        groq_constructor.assert_not_called()


class FactoryTests(unittest.TestCase):
    def test_client_uses_fixed_base_url_no_retries_no_redirects(self):
        client = build_client(KEY)
        try:
            self.assertEqual(str(client.base_url).rstrip("/"), "https://api.groq.com")
            self.assertEqual(client.max_retries, 0)
            self.assertEqual(client.timeout, 45)
            self.assertFalse(client._client.follow_redirects)
        finally:
            client.close()

    def test_environment_cannot_override_the_fixed_configuration(self):
        with patch.dict("os.environ", {"GROQ_BASE_URL": "https://other.invalid/v1",
                                       "GROQ_API_KEY": "gsk_other_marker"}):
            client = build_client(KEY)
        try:
            self.assertEqual(str(client.base_url).rstrip("/"), "https://api.groq.com")
        finally:
            client.close()

    def test_custom_headers_env_is_refused_before_any_sdk_object(self):
        for value in ("", "Authorization: gsk_other_marker\nHost: other.invalid"):
            with self.subTest(empty=not value):
                with patch.dict("os.environ", {"GROQ_CUSTOM_HEADERS": value}), \
                        patch("extraction.groq.Groq") as constructor, \
                        patch("extraction.groq.DefaultHttpxClient") as http_client:
                    with self.assertRaises(ConfigurationError):
                        build_client(KEY)
                constructor.assert_not_called()
                http_client.assert_not_called()

    def test_local_errors_are_distinct_from_validation_error(self):
        self.assertFalse(issubclass(ConfigurationError, ValidationError))
        self.assertFalse(issubclass(InputNotSendable, ValidationError))


class SdkErrorTests(unittest.TestCase):
    def run_extract_for_status(self, status, body):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(status, json=body)

        with self.assertRaises(groq.APIStatusError) as caught:
            run_extract(handler)
        self.assertEqual(len(calls), 1)
        self.assertEqual(caught.exception.status_code, status)
        self.assertNotIn("gsk_", str(caught.exception))

    def test_quota_error_is_one_shot(self):
        self.run_extract_for_status(429, {"error": {"message": "quota marker-secret-42"}})

    def test_server_errors_are_one_shot(self):
        for status in (500, 503):
            with self.subTest(status=status):
                self.run_extract_for_status(
                    status, {"error": {"message": "temporary provider body"}})

    def test_redirect_is_refused_and_never_followed(self):
        hosts, calls = [], []

        def handler(request):
            hosts.append(request.url.host)
            calls.append(1)
            return httpx.Response(302, headers={"location": "https://other.invalid/v1/chat/completions"})

        with self.assertRaises(groq.APIStatusError) as caught:
            run_extract(handler)
        self.assertEqual(caught.exception.status_code, 302)
        self.assertEqual(hosts, [HOST])
        self.assertEqual(len(calls), 1)

    def test_timeout_is_one_shot_and_typed(self):
        calls = []
        request = httpx.Request("POST", ENDPOINT)

        def handler(_request):
            calls.append(1)
            raise httpx.ConnectTimeout("simulated timeout", request=request)

        with self.assertRaises(groq.APITimeoutError) as caught:
            run_extract(handler)
        self.assertIsInstance(caught.exception, groq.APIConnectionError)
        self.assertEqual(len(calls), 1)

    def test_connection_error_is_one_shot_and_typed(self):
        calls = []
        request = httpx.Request("POST", ENDPOINT)

        def handler(_request):
            calls.append(1)
            raise httpx.ConnectError("simulated outage", request=request)

        with self.assertRaises(groq.APIConnectionError) as caught:
            run_extract(handler)
        self.assertNotIsInstance(caught.exception, groq.APITimeoutError)
        self.assertEqual(len(calls), 1)

    def test_client_is_closed_on_success_and_on_error(self):
        closed = []

        class TrackingClient(httpx.Client):
            def close(self):
                closed.append(True)
                super().close()

        for handler in (
            lambda request: httpx.Response(200, json=success_response()),
            lambda request: httpx.Response(503, json={"error": {"message": "x"}}),
        ):
            with self.subTest():
                closed.clear()
                with patch("extraction.groq.DefaultHttpxClient",
                           side_effect=lambda **kw: TrackingClient(
                               transport=httpx.MockTransport(handler), **kw)):
                    try:
                        extraction.extract({"pdf_ricevuta": document()}, KEY)
                    except groq.APIStatusError:
                        pass
                self.assertEqual(closed, [True])


class ParsingTests(unittest.TestCase):
    def completion(self, **fields):
        return json.dumps(success_response(**fields)).encode("utf-8")

    def test_valid_response_parses_fields(self):
        values = parse_completion(self.completion(costo_tratta="45.99"))
        self.assertEqual(values["numero_volo"], "AB123")
        self.assertEqual(values["costo_tratta"], "45.99")

    def test_empty_object_normalizes_to_defaults(self):
        values = parse_completion(self.completion())
        self.assertEqual(values["compagnia_aerea"], "")
        self.assertIsNone(values["data_volo"])
        self.assertIsNone(values["costo_tratta"])

    def test_invalid_completion_bodies_are_rejected(self):
        inner = '{"numero_volo": "AB123"}'
        invalid = [
            b"", b"{", b"[]", b'"plain string"', b"123",
            b'{"choices": []}', b'{"choices": "x"}', b'{"choices": [{}]}',
            b'{"choices": [{"message": "x"}]}', b'{"choices": [{"message": {}}]}',
            b'{"choices": [{"message": {"content": ""}}]}',
            b'{"choices": [{"message": {"content": 1}}]}',
            b'{"choices": [{"message": {"content": "{\"campo_extra\": \"x\"}"}}]}',
            b'{"choices": [{"message": {"content": "{\"costo_tratta\": \"abc\"}"}}]}',
            b'{"choices": [{"message": {"content": "{\"data_volo\": \"31/02/2026\"}"}}]}',
            b'{"choices": [{"message": {"content": "{\"numero_volo\": "}}]}',
            b'{"choices": [{"message": {"content": "' + inner.encode() + b'"}, "finish_reason": "length"}]}',
            # NaN and Infinity are rejected before any field validation.
            b'{"choices": [{"message": {"content": "{}"}}], "usage": NaN}',
            b'{"choices": [{"message": {"content": "{}"}}], "usage": Infinity}',
            b"x" * (extraction.MAX_RESPONSE_BYTES + 1),
        ]
        for raw in invalid:
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(ValidationError):
                    parse_completion(raw)


class _Capture(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        try:
            self.sink.append(f"[{record.name}] {self.format(record)} {record.exc_text}")
        except Exception as exc:
            self.sink.append(f"[format-error] {record.name}: {exc}")


class PrivacyFilterTests(unittest.TestCase):
    """Source-level, context-scoped filters over the real SDK loggers."""

    def capture(self):
        """Attach DEBUG capture handlers; return (targets, sink, old_state)."""
        sink = []
        targets = [logging.getLogger()] + [logging.getLogger(n) for n in DEPENDENCY_LOGGERS]
        old = [(t, t.level, list(t.handlers)) for t in targets]
        for target in targets:
            target.setLevel(logging.DEBUG)
            target.addHandler(_Capture(sink))
        return targets, sink, old

    def restore(self, targets, old):
        for (target, level, handlers), current in zip(old, targets):
            target.handlers = handlers
            target.setLevel(level)

    def test_filters_are_idempotent_and_do_not_touch_global_state(self):
        previous_disable = logging.root.manager.disable
        previous_levels = {name: logging.getLogger(name).level for name in DEPENDENCY_LOGGERS}
        try:
            restrict_dependency_logs()
            restrict_dependency_logs()
            for name in DEPENDENCY_LOGGERS:
                installed = [f for f in logging.getLogger(name).filters
                             if isinstance(f, _GroqDependencyFilter)]
                self.assertEqual(len(installed), 1)
            self.assertEqual(logging.root.manager.disable, previous_disable)
            for name, level in previous_levels.items():
                self.assertEqual(logging.getLogger(name).level, level)
        finally:
            logging.root.manager.disable = previous_disable

    def test_dependency_records_are_blocked_only_inside_the_request_scope(self):
        targets, sink, old = self.capture()
        marker = "private-provider-body-MARKER-42"
        try:
            with _groq_request_scope():
                logging.getLogger("groq._base_client").debug(
                    "Sending HTTP Request with %s", marker,
                    exc_info=(RuntimeError, RuntimeError(marker), None))
                logging.getLogger("httpx").info("HTTP Response %s", marker)
            # Outside the scope the same loggers work normally again; the
            # benign record reaches its logger and propagates to root.
            logging.getLogger("httpx").info("benign httpx record outside request")
            self.assertTrue(any("benign httpx record outside request" in line for line in sink))
            self.assertEqual(sum("benign httpx record outside request" in line for line in sink), 2)
            self.assertFalse(any(marker in line for line in sink))
        finally:
            self.restore(targets, old)

    def test_exc_info_records_are_blocked_before_formatting(self):
        targets, sink, old = self.capture()
        marker = "exc-info-MARKER-77"
        try:
            with _groq_request_scope():
                try:
                    raise RuntimeError(marker)
                except RuntimeError:
                    logging.getLogger("groq").debug("attempt failed", exc_info=True)
            self.assertEqual(sink, [])
        finally:
            self.restore(targets, old)

    def test_concurrent_sessions_do_not_leak_and_keep_benign_logs(self):
        targets, sink, old = self.capture()
        app_sink = []
        app_target = logging.getLogger("rimborso_voli")
        app_old = (app_target.level, list(app_target.handlers))
        app_target.addHandler(_Capture(app_sink))
        marker = "session-A-private-marker-42"
        errors = []

        def failing(request):
            return httpx.Response(503, json={"error": {"message": marker}})

        def succeeding(request):
            return httpx.Response(200, json=success_response())

        # One dispatcher patches the production transport once for both
        # sessions: the session identity rides the distinct credentials, so
        # the threads never race on the patched module attribute and no
        # unpatched (real network) client can be built in the race window.
        def dispatcher(request):
            if request.headers.get("authorization") == f"Bearer {KEY}":
                return failing(request)
            return succeeding(request)

        def http_client(**kwargs):
            return httpx.Client(transport=httpx.MockTransport(dispatcher), **kwargs)

        def run_session(key, level, message):
            try:
                barrier.wait(timeout=10)
                try:
                    extraction.extract({"pdf_ricevuta": document((32, 32))}, key)
                except groq.APIStatusError:
                    pass
                logging.getLogger("rimborso_voli").log(level, message)
            except Exception as exc:
                errors.append(exc)

        barrier = threading.Barrier(2)
        with patch("extraction.groq.DefaultHttpxClient", side_effect=http_client):
            thread_a = threading.Thread(target=run_session, args=(KEY, logging.WARNING,
                                                                 "benign application record session A"))
            thread_b = threading.Thread(target=run_session, args=("test-key-b", logging.INFO,
                                                                 "benign application record session B"))
            thread_a.start()
            thread_b.start()
            thread_a.join(timeout=30)
            thread_b.join(timeout=30)
        try:
            self.assertEqual(errors, [])
            joined = "\n".join(sink) + "\n".join(app_sink)
            self.assertNotIn(marker, joined)
            self.assertNotIn("test-key", joined)
            self.assertNotIn("format-error", joined)
            # Benign application records from the concurrent session survive.
            self.assertIn("benign application record session B", "\n".join(app_sink))
            self.assertIn("benign application record session A", "\n".join(app_sink))
            # No global logging state was changed by the guarded requests.
            self.assertEqual(logging.root.manager.disable, logging.NOTSET)
            for name in DEPENDENCY_LOGGERS:
                self.assertIsNotNone(logging.getLogger(name))
        finally:
            self.restore(targets, old)
            app_target.handlers = app_old[1]
            app_target.setLevel(app_old[0])

    def test_failure_bodies_do_not_reach_the_application_log(self):
        sink = []
        target = logging.getLogger("rimborso_voli")
        old_handlers, old_level = list(target.handlers), target.level
        target.addHandler(_Capture(sink))
        marker = "app-log-marker-99"
        try:
            with patch("extraction.groq.DefaultHttpxClient",
                       side_effect=lambda **kw: httpx.Client(
                           transport=httpx.MockTransport(
                               lambda request: httpx.Response(503, json={"error": {"message": marker}})),
                           **kw)):
                with self.assertRaises(groq.APIStatusError) as caught:
                    extraction.extract({"pdf_ricevuta": document()}, KEY)
            # The application maps the exception through security.log_failure.
            log_failure("extract_documents", caught.exception)
            joined = "\n".join(sink)
            self.assertIn("HTTP 503", joined)
            self.assertTrue(any(line.startswith("[rimborso_voli] extract_documents failed (")
                                for line in sink))
            self.assertNotIn(marker, joined)
        finally:
            target.handlers = old_handlers
            target.setLevel(old_level)


if __name__ == "__main__":
    unittest.main()
