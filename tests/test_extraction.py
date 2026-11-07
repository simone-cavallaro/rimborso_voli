import json
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from google import genai
from google.genai import errors as genai_errors
import httpx
from PIL import Image

from extraction import DEFAULT_MODEL, PROMPT, extract, _SdkRetryTraceFilter


def fake_client_clock(attempts, respond):
    """Build a genai Client factory wired to a deterministic HTTP transport."""
    real_client = genai.Client

    def local_client(**kwargs):
        kwargs["http_options"].client_args = {"transport": httpx.MockTransport(respond)}
        attempts.append(1)
        return real_client(**kwargs)

    return local_client


def valid_response(**fields):
    payload = {"numero_volo": "AB123"}
    payload.update(fields)
    return httpx.Response(200, json={"candidates": [{
        "content": {"role": "model", "parts": [{"text": json.dumps(payload)}]},
        "finishReason": "STOP",
    }]})


class ExtractionTests(unittest.TestCase):
    def run_extract(self, respond):
        attempts = []
        with patch("extraction.genai.Client", side_effect=fake_client_clock(attempts, respond)):
            values = extract({"pdf_ricevuta": SimpleNamespace(image=Image.new("RGB", (8, 8)))}, "test-key")
        return values, attempts

    def test_real_sdk_serializes_receipt_only_and_schema_without_network(self):
        requests = []

        def respond(request):
            requests.append(request)
            return valid_response(costo_tratta="45.99")

        values, attempts = self.run_extract(respond)
        self.assertEqual(values["numero_volo"], "AB123")
        self.assertEqual(values["costo_tratta"], "45.99")
        self.assertEqual(len(attempts), 1)
        sent = json.loads(requests[0].content)
        config = sent["generationConfig"]
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertIn("costo_tratta", config["responseSchema"]["properties"])
        # The actual request must carry the system instruction: the SDK 2.25
        # serializing a bare string is verified here, not assumed.
        self.assertIn("systemInstruction", sent)
        self.assertIn("Estrai dati", json.dumps(sent["systemInstruction"]))
        self.assertEqual(json.dumps(PROMPT), json.dumps(sent["systemInstruction"]["parts"][0]["text"]))
        self.assertEqual(len(sent["contents"][0]["parts"]), 2)
        self.assertNotIn("thinkingConfig", config)

    def test_request_targets_the_configured_model(self):
        requests = []

        def respond(request):
            requests.append(request)
            return valid_response()

        self.run_extract(respond)
        self.assertIn(DEFAULT_MODEL, str(requests[0].url))

    def test_non_string_or_empty_model_falls_back_to_default(self):
        cases = []
        for bad in (None, 123, "   "):
            requests = []

            def respond(request):
                requests.append(request)
                return valid_response()

            with patch("extraction.genai.Client", side_effect=fake_client_clock([], respond)):
                values = extract({"pdf_ricevuta": SimpleNamespace(image=Image.new("RGB", (8, 8)))},
                                 "test-key", model_name=bad)
            self.assertEqual(values["numero_volo"], "AB123")
            self.assertIn(DEFAULT_MODEL, str(requests[0].url))
            cases.append(bad)
        self.assertEqual(len(cases), 3)

    def test_retries_temporary_gemini_failure(self):
        attempts = 0

        def respond(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, json={"error": {
                    "code": 503, "message": "temporary failure", "status": "UNAVAILABLE",
                }})
            return valid_response()

        values, sent = self.run_extract(respond)
        self.assertEqual(values["numero_volo"], "AB123")
        self.assertEqual(attempts, 2)

    def test_quota_error_is_not_retried_by_the_sdk(self):
        attempts = 0

        def respond(request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(429, json={"error": {
                "code": 429, "message": "quota marker-secret-42", "status": "RESOURCE_EXHAUSTED",
            }})

        with patch("extraction.genai.Client", side_effect=fake_client_clock([], respond)):
            with self.assertRaises(genai_errors.APIError) as caught:
                extract({"pdf_ricevuta": SimpleNamespace(image=Image.new("RGB", (8, 8)))}, "test-key")
        self.assertEqual(attempts, 1)
        self.assertEqual(caught.exception.code, 429)

    def test_sdk_retry_trace_is_emitted_to_no_handler(self):
        """Real SDK, MockTransport, one synthetic temporary failure then success.

        The failure carries private markers in message and structured details;
        the SDK retry trace must not reach any handler (SDK logger, parent,
        root) at INFO or DEBUG, while a genuinely safe application event still
        reaches its logger.
        """
        marker = "private-provider-body-MARKER-42"
        attempts = 0

        def respond(request):
            nonlocal attempts
            attempts += 1
            if attempts % 2 == 1:
                return httpx.Response(503, json={"error": {
                    "code": 503, "message": marker, "status": "UNAVAILABLE",
                    "details": [{"@type": "type.invalid/SynthDetails", "leak": marker}],
                }})
            return valid_response()

        sdk_logger = logging.getLogger("google_genai._api_client")
        parent_logger = logging.getLogger("google_genai")
        root_logger = logging.getLogger()
        emitted = []

        class Capture(logging.Handler):
            def emit(self, record):
                try:
                    emitted.append(f"[root-of:{record.name}] {self.format(record)} {record.exc_text}")
                except Exception as exc:  # formatting failures must be visible too
                    emitted.append(f"[format-error] {record.name}: {exc}")

        old = {target: target.level for target in (sdk_logger, parent_logger, root_logger)}
        handlers = []
        try:
            for target in (sdk_logger, parent_logger, root_logger):
                target.setLevel(logging.DEBUG)
                handler = Capture()
                target.addHandler(handler)
                handlers.append(handler)
            with patch("extraction.genai.Client", side_effect=fake_client_clock([], respond)):
                values_a = extract({"pdf_ricevuta": SimpleNamespace(image=Image.new("RGB", (8, 8)))}, "test-key")
                values_b = extract({"pdf_ricevuta": SimpleNamespace(image=Image.new("RGB", (8, 8)))}, "test-key")
            # Other SDK records still flow to their handlers.
            sdk_logger.info("benign sdk record without provider details")
            # Application-level safe events still flow to the app logger.
            with self.assertLogs("rimborso_voli", level="INFO") as app_logs:
                logging.getLogger("rimborso_voli").info("safe application event")
        finally:
            for target, level in old.items():
                target.setLevel(level)
            for target, handler in zip((sdk_logger, parent_logger, root_logger), handlers):
                target.removeHandler(handler)
        self.assertEqual(values_a["numero_volo"], "AB123")
        self.assertEqual(values_b["numero_volo"], "AB123")
        self.assertEqual(attempts, 4)
        joined = "\n".join(emitted) + "\n".join(app_logs.output)
        self.assertNotIn(marker, joined)
        self.assertNotIn("SynthDetails", joined)
        self.assertIn("benign sdk record without provider details", joined)
        self.assertIn("safe application event", " ".join(app_logs.output))
        # The guard is stable across repeated invocations: exactly one filter.
        installed = [f for f in sdk_logger.filters if isinstance(f, _SdkRetryTraceFilter)]
        self.assertEqual(len(installed), 1)


if __name__ == "__main__":
    unittest.main()
