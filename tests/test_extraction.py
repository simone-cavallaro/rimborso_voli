import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from google import genai
import httpx
from PIL import Image

from extraction import extract


class ExtractionTests(unittest.TestCase):
    def test_real_sdk_serializes_receipt_only_and_schema_without_network(self):
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"candidates": [{
                "content": {"role": "model", "parts": [{"text": json.dumps({"numero_volo": "AB123", "costo_tratta": "45.99"})}]},
                "finishReason": "STOP",
            }]})

        real_client = genai.Client

        def local_client(**kwargs):
            kwargs["http_options"].client_args = {"transport": httpx.MockTransport(respond)}
            return real_client(**kwargs)

        with patch("extraction.genai.Client", side_effect=local_client):
            values = extract({"pdf_ricevuta": SimpleNamespace(image=Image.new("RGB", (8, 8)))}, "test-key")
        self.assertEqual(values["numero_volo"], "AB123")
        self.assertEqual(values["costo_tratta"], "45.99")
        self.assertEqual(len(requests), 1)
        config = requests[0]["generationConfig"]
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertIn("costo_tratta", config["responseSchema"]["properties"])
        self.assertIn("systemInstruction", requests[0])
        self.assertEqual(len(requests[0]["contents"][0]["parts"]), 2)

    def test_retries_temporary_gemini_failure(self):
        attempts = 0

        def respond(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, json={"error": {
                    "code": 503, "message": "temporary failure", "status": "UNAVAILABLE",
                }})
            return httpx.Response(200, json={"candidates": [{
                "content": {"role": "model", "parts": [{"text": '{"numero_volo":"AB123"}'}]},
                "finishReason": "STOP",
            }]})

        real_client = genai.Client

        def local_client(**kwargs):
            kwargs["http_options"].client_args = {"transport": httpx.MockTransport(respond)}
            return real_client(**kwargs)

        with patch("extraction.genai.Client", side_effect=local_client):
            values = extract({"pdf_ricevuta": SimpleNamespace(image=Image.new("RGB", (8, 8)))}, "test-key")
        self.assertEqual(values["numero_volo"], "AB123")
        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
