from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import groq
import httpx
from PIL import Image
from streamlit.errors import StreamlitSecretNotFoundError
from streamlit.testing.v1 import AppTest

import extraction
from domain import ValidationError
from repository import RequestsRepository
from tests.fakes import FakeClient, USER_A

ROOT = Path(__file__).resolve().parents[1]

ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


def groq_status_error(status, message="synthetic provider error"):
    request = httpx.Request("POST", ENDPOINT)
    response = httpx.Response(status, request=request)
    return groq.APIStatusError(message, response=response, body=None)


def groq_connection_error():
    return groq.APIConnectionError(message="offline",
                                   request=httpx.Request("POST", ENDPOINT))


def image_upload(color="white"):
    buffer = BytesIO()
    Image.new("RGB", (64, 64), color).save(buffer, "PNG")
    data = buffer.getvalue()
    return SimpleNamespace(size=len(data), getvalue=lambda: data)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.uploads = {}
        self.client_patch = patch("security.create_client", return_value=self.client)
        self.upload_patch = patch("streamlit.file_uploader", side_effect=lambda *args, **kwargs: self.uploads.get(kwargs["key"]))
        self.client_patch.start()
        self.upload_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.addCleanup(self.upload_patch.stop)
        self.app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15)
        self.app.secrets.update(SUPABASE_URL="https://example.supabase.co", SUPABASE_KEY="sb_publishable_test", GROQ_API_KEY="test")
        self.app.session_state["user_id"] = USER_A

    def assert_clean(self):
        self.assertEqual(list(self.app.exception), [])
        self.assertEqual(list(self.app.error), [])

    def click(self, label):
        next(button for button in self.app.button if button.label == label).click().run()

    def goto_history(self):
        self.app.radio[0].set_value("Storico rimborsi").run()

    def add_receipt(self):
        self.uploads["new_receipt"] = image_upload()
        self.app.run()

    def add_receipt_with_manual_value(self):
        self.uploads["new_receipt"] = image_upload()
        self.app.run()
        self.app.text_input(key="new_field_numero_volo").set_value("KEPT123").run()

    def assert_manual_fields_preserved(self):
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "KEPT123")
        self.assertEqual(self.client.rows, [])

    def test_navigation_is_in_the_main_body_not_the_sidebar(self):
        self.app.run()
        self.assert_clean()
        self.assertEqual([radio.label for radio in self.app.radio], ["Vai a"])
        self.assertEqual(self.app.radio[0].value, "Nuova richiesta")
        self.goto_history()
        self.assertTrue(any("Storico rimborsi" in subheader.value for subheader in self.app.subheader))

    def test_receipt_only_create_then_boarding_added_from_history(self):
        self.add_receipt()
        self.assert_clean()
        self.app.text_input(key="new_field_numero_volo").set_value("AB123")
        self.click("Salva richiesta")
        self.assert_clean()
        self.assertEqual(len(self.client.rows), 1)
        receipt = self.client.rows[0]["pdf_ricevuta"]
        self.assertIsNone(self.client.rows[0]["pdf_imbarco"])

        self.goto_history()
        self.assert_clean()
        self.assertEqual(self.client.bucket.downloads, 0)
        self.click("Modifica / aggiungi carta d'imbarco")
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="edit_field_numero_volo").value, "AB123")
        self.uploads["edit_boarding"] = image_upload("blue")
        self.app.run()
        self.app.text_input(key="edit_field_numero_volo").set_value("AB456")
        self.click("Salva modifiche")
        self.assert_clean()
        self.assertEqual(self.client.rows[0]["pdf_ricevuta"], receipt)
        self.assertTrue(self.client.rows[0]["pdf_imbarco"])
        self.assertEqual(self.client.rows[0]["numero_volo"], "AB456")
        self.assertEqual(self.client.bucket.downloads, 0)

    def test_file_change_clears_previous_extraction(self):
        self.add_receipt()
        with patch("extraction.extract", return_value={"numero_volo": "OLD123"}), \
                patch("ocr_fallback.extract_local") as ocr:
            self.click("Leggi i nuovi documenti con AI")
        ocr.assert_not_called()
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "OLD123")
        self.uploads["new_receipt"] = image_upload("red")
        self.app.run()
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "")

    def test_groq_success_does_not_call_ocr_or_save(self):
        self.add_receipt()
        with patch("extraction.extract", return_value={"numero_volo": "GRO777"}), \
                patch("ocr_fallback.extract_local") as ocr:
            self.click("Leggi i nuovi documenti con AI")
        ocr.assert_not_called()
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "GRO777")
        self.assertTrue(any("Groq" in success.value for success in self.app.success))
        self.assertEqual(self.client.rows, [])

    def test_failed_ai_still_allows_manual_saving(self):
        self.add_receipt_with_manual_value()
        error = groq_status_error(503, "secret-provider-details")
        with patch("extraction.extract", side_effect=error), \
                patch("ocr_fallback.extract_local", return_value={}):
            self.click("Leggi i nuovi documenti con AI")
        self.assertEqual(list(self.app.exception), [])
        self.assertTrue(any("temporaneamente non disponibile" in warning.value for warning in self.app.warning))
        self.assertTrue(any("compila i campi manualmente" in warning.value for warning in self.app.warning))
        self.assert_manual_fields_preserved()
        self.click("Salva richiesta")
        self.assert_clean()
        self.assertEqual(self.client.rows[0]["numero_volo"], "KEPT123")

    def test_missing_groq_key_explains_ocr_fallback_without_api_call(self):
        self.add_receipt()
        self.app.secrets["GROQ_API_KEY"] = ""
        with patch("extraction.extract") as api, \
                patch("ocr_fallback.extract_local", return_value={"numero_volo": "OCR123"}):
            self.click("Leggi i nuovi documenti con AI")
        api.assert_not_called()
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "OCR123")
        self.assertTrue(any("non è configurata" in warning.value for warning in self.app.warning))
        self.assertTrue(any("OCR locale" in warning.value for warning in self.app.warning))

    def test_quota_error_is_a_distinct_category(self):
        self.add_receipt()
        error = groq_status_error(429, "quota-exhausted-secret-marker")
        with patch("extraction.extract", side_effect=error), \
                patch("ocr_fallback.extract_local", return_value={"numero_volo": "OCRQ1"}):
            self.click("Leggi i nuovi documenti con AI")
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "OCRQ1")
        self.assertTrue(any("Quota o limite del servizio" in warning.value for warning in self.app.warning))
        self.assertFalse(any(warning.value == "Groq è temporaneamente non disponibile: i dati sono stati letti con l'OCR locale. Riprova più tardi."
                             for warning in self.app.warning))

    def test_503_with_failed_ocr_keeps_both_diagnoses(self):
        self.add_receipt_with_manual_value()
        error = groq_status_error(503, "provider-raw-details")
        with patch("extraction.extract", side_effect=error), \
                patch("ocr_fallback.extract_local", side_effect=RuntimeError("ocr broken")):
            self.click("Leggi i nuovi documenti con AI")
        self.assertEqual(list(self.app.exception), [])
        rendered = " ".join(item.value for item in self.app.error)
        self.assertIn("temporaneamente non disponibile", rendered)
        self.assertIn("Anche l'OCR locale non è riuscito", rendered)
        self.assert_manual_fields_preserved()

    def test_network_error_with_failed_ocr_keeps_reason(self):
        self.add_receipt_with_manual_value()
        with patch("extraction.extract", side_effect=groq_connection_error()), \
                patch("ocr_fallback.extract_local", side_effect=RuntimeError("ocr broken")):
            self.click("Leggi i nuovi documenti con AI")
        self.assertEqual(list(self.app.exception), [])
        rendered = " ".join(item.value for item in self.app.error)
        self.assertIn("non è raggiungibile", rendered)
        self.assertIn("Anche l'OCR locale non è riuscito", rendered)
        self.assert_manual_fields_preserved()

    def test_non_string_groq_model_falls_back_to_default(self):
        import extraction
        self.add_receipt()
        self.app.secrets["GROQ_MODEL"] = 12345
        captured = {}
        with patch("extraction.extract") as api:
            def capture(documents, api_key, model_name=None):
                captured["model"] = model_name
                return {"numero_volo": "OK"}
            api.side_effect = capture
            self.click("Leggi i nuovi documenti con AI")
        self.assert_clean()
        self.assertEqual(captured["model"], extraction.DEFAULT_MODEL)

    # --- Secrets: distinguish absence from malformed source (structured ids) ---

    class _FakeSecrets:
        def __init__(self, exc=None, values=None):
            self.exc = exc
            self.values = values

        def get(self, key):
            if self.exc is not None:
                raise self.exc
            return self.values.get(key)

    def _settings_with(self, secrets):
        import app as app_module
        with patch("app.st", SimpleNamespace(secrets=secrets)):
            return app_module.groq_settings()

    def test_no_secrets_found_and_plain_file_error_mean_absence(self):
        no_file = StreamlitSecretNotFoundError(
            "No secrets found. Valid paths for a secrets.toml file or secret directories are: {file_paths}",
            file_paths="/a, /b", error_id="no-secrets-found")
        self.assertEqual(self._settings_with(self._FakeSecrets(exc=no_file))[2], "absent")
        self.assertEqual(self._settings_with(self._FakeSecrets(exc=FileNotFoundError("gone")))[2], "absent")

    def test_malformed_secrets_id_is_a_distinct_blocking_mode(self):
        malformed = StreamlitSecretNotFoundError(
            "Error parsing secrets file at {path}: {error}",
            path="/demo/secrets.toml", error="toml private-marker-toml-err",
            error_id="failed-parsing-secrets-file")
        api_key, model, mode = self._settings_with(self._FakeSecrets(exc=malformed))
        self.assertIsNone(api_key)
        self.assertEqual(mode, "malformed")
        self.assertEqual(model, extraction.DEFAULT_MODEL)
        invalid_path = StreamlitSecretNotFoundError(
            "Invalid secrets path: {path}: path is not a .toml file or a directory",
            path="/demo", error_id="invalid-secrets-path")
        self.assertEqual(self._settings_with(self._FakeSecrets(exc=invalid_path))[2], "malformed")
        unknown = StreamlitSecretNotFoundError("future id", error_id="not-yet-known-id")
        self.assertEqual(self._settings_with(self._FakeSecrets(exc=unknown))[2], "malformed")

    def test_configured_secrets_report_configured_mode(self):
        secrets = self._FakeSecrets(values={"GROQ_API_KEY": " k ", "GROQ_MODEL": " models/demo "})
        self.assertEqual(self._settings_with(secrets), ("k", "models/demo", "configured"))

    def test_malformed_secrets_flow_shows_fixed_message_without_ocr_or_save(self):
        self.add_receipt_with_manual_value()
        malformed = StreamlitSecretNotFoundError(
            "Error parsing secrets file at {path}: {error}",
            path="/demo/.streamlit/secrets.toml", error="toml private-marker-toml-err",
            error_id="failed-parsing-secrets-file")

        class FakeSecretsStore:
            """Synthetic structured source placed where AppTest installs st.secrets.

            AppTest replaces the global st.secrets with a Secrets() instance
            built from this module, so patching that class exercises the real
            helper (exception included) inside the complete app run. SUPABASE
            values pass through; the Groq ones raise the structured error. No
            real API call can occur: the analysis is blocked before extraction.
            """

            def __init__(self, *args, **kwargs):
                self._secrets = None
                self.raise_keys = ("GROQ_API_KEY", "GROQ_MODEL")

            def __getitem__(self, key):
                if key in self.raise_keys:
                    raise malformed
                return self._secrets[key]

            def get(self, key, default=None):
                if key in self.raise_keys:
                    raise malformed
                try:
                    return self[key]
                except KeyError:
                    return default

        with patch("streamlit.testing.v1.app_test.Secrets", FakeSecretsStore), \
                patch("ocr_fallback.extract_local") as ocr, \
                patch("extraction.extract", side_effect=RuntimeError("no api call expected")):
            self.click("Leggi i nuovi documenti con AI")
        ocr.assert_not_called()
        self.assertEqual(list(self.app.exception), [])
        rendered = " ".join(item.value for item in self.app.error)
        self.assertIn("impostazioni non leggibili", rendered)
        self.assertNotIn("failed-parsing-secrets-file", rendered)
        self.assertNotIn("toml private-marker", rendered)
        self.assert_manual_fields_preserved()

    def test_malformed_secrets_caption_shows_no_exception_details(self):
        malformed = StreamlitSecretNotFoundError(
            "Error parsing secrets file at {path}: {error}",
            path="/demo/.streamlit/secrets.toml", error="toml private-marker-caption-err",
            error_id="failed-parsing-secrets-file")

        class FakeSecretsStore:
            def __init__(self, *args, **kwargs):
                self._secrets = None
                self.raise_keys = ("GROQ_API_KEY",)

            def __getitem__(self, key):
                if key in self.raise_keys:
                    raise malformed
                return self._secrets[key]

            def get(self, key, default=None):
                if key in self.raise_keys:
                    raise malformed
                try:
                    return self[key]
                except KeyError:
                    return default

        with patch("streamlit.testing.v1.app_test.Secrets", FakeSecretsStore):
            self.app.run()
        captions = [element.value for element in self.app.caption]
        self.assertTrue(any("configurazione malformata" in caption for caption in captions))
        self.assertFalse(any("failed-parsing-secrets-file" in caption for caption in captions))

    def test_permanent_auth_error_is_not_masked_as_ocr_success(self):
        self.add_receipt_with_manual_value()
        error = groq_status_error(401, "raw-provider-body-marker")
        with patch("extraction.extract", side_effect=error), \
                patch("ocr_fallback.extract_local") as ocr:
            self.click("Leggi i nuovi documenti con AI")
        ocr.assert_not_called()
        self.assertEqual(list(self.app.exception), [])
        rendered = " ".join(item.value for item in self.app.error)
        self.assertIn("configurazione o autorizzazione da verificare", rendered)
        self.assertNotIn("raw-provider-body-marker", rendered)
        self.assertFalse(self.app.warning)
        self.assert_manual_fields_preserved()

    def test_permanent_model_error_is_not_masked(self):
        self.add_receipt_with_manual_value()
        error = groq_status_error(404, "model-not-found-marker")
        with patch("extraction.extract", side_effect=error), \
                patch("ocr_fallback.extract_local") as ocr:
            self.click("Leggi i nuovi documenti con AI")
        ocr.assert_not_called()
        self.assertEqual(list(self.app.exception), [])
        rendered = " ".join(item.value for item in self.app.error)
        self.assertIn("modello o configurazione non accessibili da verificare", rendered)
        self.assertNotIn("model-not-found-marker", rendered)
        self.assert_manual_fields_preserved()

    def test_permanent_request_error_is_not_masked(self):
        self.add_receipt_with_manual_value()
        for status in (400, 401, 403):
            with self.subTest(status=status):
                error = groq_status_error(status, f"bad request marker {status}")
                self.app.session_state["last_analysis"] = -1000.0
                with patch("extraction.extract", side_effect=error), \
                        patch("ocr_fallback.extract_local") as ocr:
                    self.click("Leggi i nuovi documenti con AI")
                ocr.assert_not_called()
                self.assertEqual(list(self.app.exception), [])
                rendered = " ".join(item.value for item in self.app.error)
                self.assertIn("configurazione o autorizzazione da verificare", rendered)
                self.assertNotIn("bad request marker", rendered)
                self.assert_manual_fields_preserved()

    def test_oversized_input_blocks_analysis_without_ocr_or_call(self):
        self.add_receipt_with_manual_value()
        with patch("extraction.extract", side_effect=extraction.InputNotSendable()), \
                patch("ocr_fallback.extract_local") as ocr:
            self.click("Leggi i nuovi documenti con AI")
        ocr.assert_not_called()
        self.assertEqual(list(self.app.exception), [])
        rendered = " ".join(item.value for item in self.app.error)
        self.assertIn("troppo grandi per l'invio", rendered)
        self.assertIn("compila i campi manualmente", rendered)
        self.assertNotIn("risposta non valida", rendered)
        self.assert_manual_fields_preserved()

    def test_local_configuration_error_blocks_analysis_without_ocr(self):
        self.add_receipt_with_manual_value()
        with patch("extraction.extract", side_effect=extraction.ConfigurationError()), \
                patch("ocr_fallback.extract_local") as ocr:
            self.click("Leggi i nuovi documenti con AI")
        ocr.assert_not_called()
        self.assertEqual(list(self.app.exception), [])
        rendered = " ".join(item.value for item in self.app.error)
        self.assertIn("configurazione o autorizzazione da verificare", rendered)
        self.assert_manual_fields_preserved()

    def test_network_error_falls_back_to_ocr(self):
        self.add_receipt()
        with patch("extraction.extract", side_effect=groq_connection_error()), \
                patch("ocr_fallback.extract_local", return_value={"numero_volo": "OCR456"}):
            self.click("Leggi i nuovi documenti con AI")
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "OCR456")
        self.assertTrue(any("non è raggiungibile" in warning.value or "Impossibile raggiungere" in warning.value
                            for warning in self.app.warning))

    def test_invalid_groq_response_falls_back_to_ocr(self):
        self.add_receipt()
        error = ValidationError("risposta non valida")
        with patch("extraction.extract", side_effect=error), \
                patch("ocr_fallback.extract_local", return_value={"numero_volo": "OCR000"}):
            self.click("Leggi i nuovi documenti con AI")
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "OCR000")
        self.assertTrue(any("dati validi" in warning.value for warning in self.app.warning))

    def test_provider_details_never_reach_ui_or_log(self):
        self.add_receipt()
        error = groq_status_error(500, "raw-500-provider-secret-marker")
        with patch("extraction.extract", side_effect=error), \
                patch("ocr_fallback.extract_local", return_value={"numero_volo": "OCRX1"}), \
                self.assertLogs("rimborso_voli", level="WARNING") as logs:
            self.click("Leggi i nuovi documenti con AI")
        self.assert_clean()
        rendered = " ".join(item.value for item in self.app.warning)
        self.assertNotIn("raw-500-provider-secret-marker", rendered)
        joined = " ".join(logs.output)
        self.assertNotIn("raw-500-provider-secret-marker", joined)
        self.assertIn("extract_documents failed (APIStatusError, HTTP 500)", joined)
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "OCRX1")
        self.assertEqual(self.client.rows, [])

    def test_invalid_cost_blocks_save_without_upload(self):
        self.add_receipt()
        self.app.text_input(key="new_field_costo_tratta").set_value("NaN")
        self.click("Salva richiesta")
        self.assertTrue(self.app.error)
        self.assertEqual(self.client.rows, [])
        self.assertEqual(self.client.bucket.uploads, 0)

    def test_download_on_demand_and_delete_confirmation(self):
        repo = RequestsRepository(self.client, USER_A)
        row, _ = repo.create({}, {"pdf_ricevuta": SimpleNamespace(pdf=b"%PDF-test")}, str(uuid4()))
        self.app.run()
        self.goto_history()
        self.assertEqual(self.client.bucket.downloads, 0)
        delete = next(button for button in self.app.button if button.label == "Elimina richiesta")
        self.assertTrue(delete.disabled)
        self.click("Prepara download: Ricevuta")
        self.assert_clean()
        self.assertEqual(self.client.bucket.downloads, 1)
        self.app.checkbox(key=f"confirm_{row['id']}").check().run()
        self.click("Elimina richiesta")
        self.assert_clean()
        self.assertEqual(self.client.rows, [])

    def test_logout_from_main_body_clears_sensitive_state(self):
        self.app.run()
        buttons = [button.label for button in self.app.button]
        self.assertIn("Esci", buttons)
        self.app.session_state["download"] = (1, "pdf_ricevuta", b"private")
        self.click("Esci")
        self.assertNotIn("user_id", self.app.session_state)
        self.assertNotIn("download", self.app.session_state)
        self.assertEqual(list(self.app.exception), [])


if __name__ == "__main__":
    unittest.main()
