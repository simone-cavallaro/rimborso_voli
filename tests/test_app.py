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

from rimborso_voli import extraction
from rimborso_voli.domain import ValidationError
from rimborso_voli.repository import RequestsRepository
from tests.fakes import FakeClient, USER_A, USER_B

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
        self.client_patch = patch("rimborso_voli.security.create_client", return_value=self.client)
        self.upload_patch = patch("streamlit.file_uploader", side_effect=lambda *args, **kwargs: self.uploads.get(kwargs["key"]))
        self.client_patch.start()
        self.upload_mock = self.upload_patch.start()
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
        self.app.radio[0].set_value("Storico richieste").run()

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
        self.assertTrue(any("Storico richieste" in subheader.value for subheader in self.app.subheader))

    def test_empty_history_opens_new_request_and_keeps_partial_draft(self):
        self.app.run()
        self.app.text_input(key="new_field_numero_volo").set_value("BOZZA123").run()
        self.app.text_input(key="new_field_data_volo").set_value("17/11/").run()
        self.goto_history()
        self.assertTrue(any("Non hai ancora salvato richieste" in item.value for item in self.app.info))
        self.app.button(key="history_create").click().run()
        self.assert_clean()
        self.assertEqual(self.app.radio[0].value, "Nuova richiesta")
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "BOZZA123")
        self.assertEqual(self.app.text_input(key="new_field_data_volo").value, "17/11/")
        self.assertEqual(self.client.rows, [])

    def test_both_draft_documents_and_ai_values_survive_empty_selectors(self):
        self.uploads.update(new_receipt=image_upload(), new_boarding=image_upload("blue"))
        self.app.run()
        with patch("rimborso_voli.extraction.extract", return_value={"numero_volo": "AI123"}):
            self.click("Leggi i nuovi documenti con AI")
        self.app.text_input(key="new_field_data_volo").set_value("17/11/").run()
        digests = {field: self.app.session_state[f"new_prepared_{field}"].digest
                   for field in ("pdf_ricevuta", "pdf_imbarco")}
        with patch("rimborso_voli.extraction.extract") as ai, patch("rimborso_voli.ocr_fallback.extract_local") as ocr:
            self.goto_history()
            # Model a selector lost on navigation, without an on_change event.
            self.uploads.clear()
            self.app.button(key="history_create").click().run()
        ai.assert_not_called()
        ocr.assert_not_called()
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "AI123")
        self.assertEqual(self.app.text_input(key="new_field_data_volo").value, "17/11/")
        for field, digest in digests.items():
            self.assertEqual(self.app.session_state[f"new_prepared_{field}"].digest, digest)
        self.assertEqual(sum("conservata nella bozza" in item.value for item in self.app.text), 2)
        self.assertEqual(self.client.rows, [])
        self.assertEqual(self.client.bucket.uploads, 0)

    def test_explicit_removal_cannot_resurrect_old_selector_values(self):
        self.uploads.update(new_receipt=image_upload(), new_boarding=image_upload("blue"))
        self.app.run()
        self.app.button(key="new_remove_pdf_ricevuta").click().run()
        self.assert_clean()
        self.assertNotIn("new_prepared_pdf_ricevuta", self.app.session_state)
        self.assertIn("new_prepared_pdf_imbarco", self.app.session_state)
        self.app.run()  # The old fake selector still returns its original file.
        self.assertNotIn("new_prepared_pdf_ricevuta", self.app.session_state)
        self.app.button(key="new_remove_pdf_imbarco").click().run()
        self.goto_history()
        self.uploads.clear()
        self.app.button(key="history_create").click().run()
        self.assert_clean()
        self.assertNotIn("new_prepared_pdf_ricevuta", self.app.session_state)
        self.assertNotIn("new_prepared_pdf_imbarco", self.app.session_state)
        self.assertTrue(self.app.button(key="new_analyze").disabled)

    def test_retained_documents_offer_no_selector_until_explicit_removal(self):
        self.uploads.update(new_receipt=image_upload(), new_boarding=image_upload("blue"))
        self.app.run()
        self.upload_mock.reset_mock()
        self.app.run()
        self.assert_clean()
        self.upload_mock.assert_not_called()
        self.assertEqual(sum("conservata nella bozza" in item.value for item in self.app.text), 2)
        self.app.button(key="new_remove_pdf_imbarco").click().run()
        self.upload_mock.reset_mock()
        self.app.run()
        self.assertEqual([call.kwargs["key"] for call in self.upload_mock.call_args_list], ["new_boarding_1"])
        self.assertNotIn("new_prepared_pdf_imbarco", self.app.session_state)
        self.assertIn("new_prepared_pdf_ricevuta", self.app.session_state)

    def test_no_event_after_removal_cannot_reuse_the_previous_receipt(self):
        self.uploads.update(new_receipt=image_upload(), new_boarding=image_upload("blue"))
        self.app.run()
        self.app.button(key="new_remove_pdf_ricevuta").click().run()
        # A client rejection can send neither a file nor a callback to Python.
        # Keep the new selector empty; do not invent an oversize server event.
        self.goto_history()
        self.uploads.clear()
        self.app.button(key="history_create").click().run()
        self.assert_clean()
        self.assertNotIn("new_prepared_pdf_ricevuta", self.app.session_state)
        self.assertNotIn("new_upload_error_pdf_ricevuta", self.app.session_state)
        with patch("rimborso_voli.extraction.extract", return_value={}) as ai:
            self.click("Leggi i nuovi documenti con AI")
        self.assertEqual(set(ai.call_args.args[0]), {"pdf_imbarco"})
        self.click("Salva richiesta")
        self.assertEqual(list(self.app.exception), [])
        self.assertTrue(self.app.error)  # A new request still needs its receipt.
        self.assertEqual(self.client.rows, [])
        self.assertEqual(self.client.bucket.uploads, 0)

    def test_empty_edit_replacement_does_not_reuse_the_removed_draft_copy(self):
        repo = RequestsRepository(self.client, USER_A)
        row, _ = repo.create({}, {"pdf_ricevuta": SimpleNamespace(pdf=b"%PDF-original")}, str(uuid4()))
        self.app.run()
        self.goto_history()
        self.click("Modifica / aggiungi carta d'imbarco")
        self.uploads["edit_receipt"] = image_upload("red")
        self.app.run()
        self.upload_mock.reset_mock()
        self.app.run()
        self.assertNotIn("edit_receipt", [call.kwargs["key"] for call in self.upload_mock.call_args_list])
        self.app.button(key="edit_remove_pdf_ricevuta").click().run()
        self.app.radio[0].set_value("Nuova richiesta").run()
        self.uploads.clear()
        self.goto_history()
        self.assertNotIn("edit_prepared_pdf_ricevuta", self.app.session_state)
        self.click("Salva modifiche")
        self.assert_clean()
        self.assertEqual(self.client.rows[0]["pdf_ricevuta"], row["pdf_ricevuta"])
        self.assertEqual(self.client.bucket.uploads, 1)

    def test_explicit_selector_clear_removes_the_rejected_upload_error(self):
        self.uploads["new_receipt"] = SimpleNamespace(size=3, getvalue=lambda: b"bad")
        self.app.run()
        self.assertTrue(self.app.error)
        self.uploads.clear()
        # Simulate the uploader on_change event for its native removal action.
        self.app.session_state["new_upload_changed_pdf_ricevuta"] = True
        self.app.run()
        self.assert_clean()
        self.assertNotIn("new_prepared_pdf_ricevuta", self.app.session_state)
        self.assertNotIn("new_upload_error_pdf_ricevuta", self.app.session_state)
        self.assertTrue(self.app.button(key="new_analyze").disabled)

    def test_replacement_after_removal_uses_only_the_new_document(self):
        self.add_receipt()
        old_digest = self.app.session_state["new_prepared_pdf_ricevuta"].digest
        self.app.button(key="new_remove_pdf_ricevuta").click().run()
        self.uploads["new_receipt_1"] = image_upload("red")
        self.app.run()
        new_document = self.app.session_state["new_prepared_pdf_ricevuta"]
        self.assertNotEqual(new_document.digest, old_digest)
        self.goto_history()
        self.uploads.clear()
        self.app.button(key="history_create").click().run()
        self.click("Salva richiesta")
        self.assert_clean()
        row = self.client.rows[0]
        self.assertEqual(self.client.bucket.files[row["pdf_ricevuta"]], new_document.pdf)
        self.assertNotIn("new_prepared_pdf_ricevuta", self.app.session_state)
        self.assertNotIn("new_upload_error_pdf_ricevuta", self.app.session_state)

    def test_invalid_replacement_stays_blocking_after_navigation_until_removed(self):
        self.add_receipt()
        self.app.button(key="new_remove_pdf_ricevuta").click().run()
        data = b"not-a-png-synthetic"
        self.uploads["new_receipt_1"] = SimpleNamespace(size=len(data), getvalue=lambda: data)
        self.app.run()
        self.assertNotIn("new_prepared_pdf_ricevuta", self.app.session_state)
        self.assertTrue(self.app.error)
        self.goto_history()
        self.uploads.clear()
        self.app.button(key="history_create").click().run()
        self.assertEqual(list(self.app.exception), [])
        self.assertTrue(self.app.error)
        self.assertTrue(self.app.button(key="new_analyze").disabled)
        self.assertTrue(self.app.button(key="new_save").disabled)
        self.app.button(key="new_remove_pdf_ricevuta").click().run()
        self.assert_clean()
        self.assertNotIn("new_upload_error_pdf_ricevuta", self.app.session_state)
        self.assertFalse(self.app.button(key="new_save").disabled)
        self.assertEqual(self.client.rows, [])
        self.assertEqual(self.client.bucket.uploads, 0)

    def test_server_received_oversized_upload_is_rejected_after_explicit_removal(self):
        self.add_receipt()
        self.app.button(key="new_remove_pdf_ricevuta").click().run()
        # This covers server validation, not Streamlit's browser-side rejection.
        self.uploads["new_receipt_1"] = SimpleNamespace(size=10 * 1024 * 1024 + 1,
                                                     getvalue=lambda: self.fail("must reject size before reading"))
        self.app.run()
        self.assertEqual(list(self.app.exception), [])
        self.assertNotIn("new_prepared_pdf_ricevuta", self.app.session_state)
        self.assertTrue(any("massimo 10 MB" in item.value for item in self.app.error))
        self.assertTrue(self.app.button(key="new_save").disabled)
        self.uploads["new_receipt_1"] = image_upload("red")
        self.app.run()
        self.assert_clean()
        self.assertIn("new_prepared_pdf_ricevuta", self.app.session_state)
        self.assertFalse(self.app.button(key="new_save").disabled)

    def test_edit_replacements_survive_view_change_and_are_saved_explicitly(self):
        repo = RequestsRepository(self.client, USER_A)
        row, _ = repo.create({}, {"pdf_ricevuta": SimpleNamespace(pdf=b"%PDF-original")}, str(uuid4()))
        self.app.run()
        self.goto_history()
        self.click("Modifica / aggiungi carta d'imbarco")
        self.uploads.update(edit_receipt=image_upload("red"), edit_boarding=image_upload("blue"))
        self.app.run()
        self.app.text_input(key="edit_field_numero_volo").set_value("BOZZA123").run()
        self.app.radio[0].set_value("Nuova richiesta").run()
        self.uploads.clear()
        self.goto_history()
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="edit_field_numero_volo").value, "BOZZA123")
        for field in ("pdf_ricevuta", "pdf_imbarco"):
            self.assertIn(f"edit_prepared_{field}", self.app.session_state)
        self.assertEqual(self.client.rows[0]["pdf_ricevuta"], row["pdf_ricevuta"])
        self.assertEqual(self.client.bucket.uploads, 1)
        self.click("Salva modifiche")
        self.assert_clean()
        self.assertNotEqual(self.client.rows[0]["pdf_ricevuta"], row["pdf_ricevuta"])
        self.assertTrue(self.client.rows[0]["pdf_imbarco"])
        self.assertEqual(self.client.bucket.uploads, 3)
        for field in ("pdf_ricevuta", "pdf_imbarco"):
            self.assertNotIn(f"edit_prepared_{field}", self.app.session_state)

    def test_removing_edit_uploads_preserves_both_saved_documents(self):
        repo = RequestsRepository(self.client, USER_A)
        row, _ = repo.create({}, {"pdf_ricevuta": SimpleNamespace(pdf=b"%PDF-receipt"),
                                 "pdf_imbarco": SimpleNamespace(pdf=b"%PDF-boarding")}, str(uuid4()))
        self.app.run()
        self.goto_history()
        self.click("Modifica / aggiungi carta d'imbarco")
        self.uploads.update(edit_receipt=image_upload("red"), edit_boarding=image_upload("blue"))
        self.app.run()
        for field in ("pdf_ricevuta", "pdf_imbarco"):
            self.app.button(key=f"edit_remove_{field}").click().run()
        self.click("Salva modifiche")
        self.assert_clean()
        for field in ("pdf_ricevuta", "pdf_imbarco"):
            self.assertEqual(self.client.rows[0][field], row[field])
            self.assertIn(row[field], self.client.bucket.files)
            self.assertNotIn(f"edit_prepared_{field}", self.app.session_state)
        self.assertEqual(self.client.bucket.uploads, 2)

    def test_invalid_edit_upload_blocks_save_until_explicit_removal(self):
        repo = RequestsRepository(self.client, USER_A)
        row, _ = repo.create({}, {"pdf_ricevuta": SimpleNamespace(pdf=b"%PDF-original")}, str(uuid4()))
        self.app.run()
        self.goto_history()
        self.click("Modifica / aggiungi carta d'imbarco")
        self.uploads["edit_receipt"] = SimpleNamespace(size=3, getvalue=lambda: b"bad")
        self.app.run()
        self.assertTrue(self.app.error)
        self.assertTrue(self.app.button(key="edit_save").disabled)
        self.assertNotIn("edit_prepared_pdf_ricevuta", self.app.session_state)
        self.app.radio[0].set_value("Nuova richiesta").run()
        self.uploads.clear()
        self.goto_history()
        self.assertTrue(self.app.error)
        self.assertTrue(self.app.button(key="edit_save").disabled)
        self.app.button(key="edit_remove_pdf_ricevuta").click().run()
        self.click("Salva modifiche")
        self.assert_clean()
        self.assertEqual(self.client.rows[0]["pdf_ricevuta"], row["pdf_ricevuta"])
        self.assertNotIn("edit_upload_error_pdf_ricevuta", self.app.session_state)
        self.assertEqual(self.client.bucket.uploads, 1)

    def test_reset_clears_valid_documents_and_persistent_upload_errors(self):
        self.uploads.update(new_receipt=image_upload(),
                            new_boarding=SimpleNamespace(size=3, getvalue=lambda: b"bad"))
        self.app.run()
        self.assertIn("new_prepared_pdf_ricevuta", self.app.session_state)
        self.assertIn("new_upload_error_pdf_imbarco", self.app.session_state)
        self.app.checkbox(key="new_confirm_reset").check().run()
        self.uploads.clear()
        self.app.button(key="new_reset").click().run()
        self.assert_clean()
        for field in ("pdf_ricevuta", "pdf_imbarco"):
            for kind in ("prepared", "upload_error", "upload_changed"):
                self.assertNotIn(f"new_{kind}_{field}", self.app.session_state)
        self.assertEqual(self.client.rows, [])

    def test_account_is_plain_native_text_without_markdown_interpretation(self):
        self.client.user.email = "persona+[demo]<test>@example.invalid"
        self.app.run()
        self.assert_clean()
        self.assertIn("Accesso: persona+[demo]<test>@example.invalid", [item.value for item in self.app.text])
        self.assertFalse(any("Accesso:" in item.value for item in self.app.caption))

    def test_documents_do_not_cross_user_sessions(self):
        self.add_receipt()
        other_client = FakeClient(USER_B)
        other_app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15)
        other_app.secrets.update(SUPABASE_URL="https://example.supabase.co", SUPABASE_KEY="sb_publishable_test", GROQ_API_KEY="test")
        other_app.session_state["user_id"] = USER_B
        self.uploads.clear()
        with patch("rimborso_voli.security.create_client", return_value=other_client):
            other_app.run()
        self.assertEqual(list(other_app.exception), [])
        self.assertEqual(list(other_app.error), [])
        self.assertIn("new_prepared_pdf_ricevuta", self.app.session_state)
        self.assertNotIn("new_prepared_pdf_ricevuta", other_app.session_state)
        self.assertFalse(any("conservata nella bozza" in item.value for item in other_app.text))
        self.assertEqual(other_client.bucket.uploads, 0)
        self.assertEqual(other_client.rows, [])

    def test_reset_requires_confirmation_and_does_not_touch_saved_records(self):
        repo = RequestsRepository(self.client, USER_A)
        row, _ = repo.create({}, {"pdf_ricevuta": SimpleNamespace(pdf=b"%PDF-test")}, str(uuid4()))
        self.add_receipt_with_manual_value()
        request_id = self.app.session_state["new_request_id"]
        self.assertTrue(self.app.button(key="new_reset").disabled)
        self.app.checkbox(key="new_confirm_reset").check().run()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "KEPT123")
        self.assertFalse(self.app.button(key="new_reset").disabled)
        # The uploader double must also reflect Streamlit clearing its widget.
        self.uploads.clear()
        self.app.button(key="new_reset").click().run()
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "")
        self.assertNotEqual(self.app.session_state["new_request_id"], request_id)
        self.assertFalse(self.app.checkbox(key="new_confirm_reset").value)
        self.assertNotIn("new_prepared_pdf_ricevuta", self.app.session_state)
        self.assertEqual([saved["id"] for saved in self.client.rows], [row["id"]])
        self.assertIn(row["pdf_ricevuta"], self.client.bucket.files)

    def test_upload_change_keeps_manual_values_including_partial_date(self):
        self.add_receipt_with_manual_value()
        self.app.text_input(key="new_field_data_volo").set_value("17/11/").run()
        self.app.button(key="new_remove_pdf_ricevuta").click().run()
        self.uploads["new_receipt_1"] = image_upload("red")
        self.app.run()
        self.assert_clean()
        self.assert_manual_fields_preserved()
        self.assertEqual(self.app.text_input(key="new_field_data_volo").value, "17/11/")

    def test_upload_change_keeps_corrected_ai_fields_and_clears_stale_suggestions(self):
        self.add_receipt()
        with patch("rimborso_voli.extraction.extract", return_value={"numero_volo": "AI123", "compagnia_aerea": "Demo Air"}):
            self.click("Leggi i nuovi documenti con AI")
        self.app.text_input(key="new_field_numero_volo").set_value("CORRETTO456").run()
        self.app.button(key="new_remove_pdf_ricevuta").click().run()
        self.uploads["new_receipt_1"] = image_upload("red")
        self.app.run()
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "CORRETTO456")
        self.assertEqual(self.app.text_input(key="new_field_compagnia_aerea").value, "")

    def test_edit_without_upload_preserves_documents_and_italian_formats(self):
        repo = RequestsRepository(self.client, USER_A)
        row, _ = repo.create(
            {"data_volo": "2026-11-17", "costo_tratta": "123.45"},
            {"pdf_ricevuta": SimpleNamespace(pdf=b"%PDF-receipt"),
             "pdf_imbarco": SimpleNamespace(pdf=b"%PDF-boarding")}, str(uuid4()))
        self.app.run()
        self.goto_history()
        self.click("Modifica / aggiungi carta d'imbarco")
        self.assertEqual(self.app.text_input(key="edit_field_data_volo").value, "17/11/2026")
        self.assertEqual(self.app.text_input(key="edit_field_costo_tratta").value, "123,45")
        self.app.text_input(key="edit_field_numero_volo").set_value("DEMO123").run()
        self.click("Salva modifiche")
        self.assert_clean()
        saved = self.client.rows[0]
        self.assertEqual(saved["numero_volo"], "DEMO123")
        self.assertEqual(saved["pdf_ricevuta"], row["pdf_ricevuta"])
        self.assertEqual(saved["pdf_imbarco"], row["pdf_imbarco"])
        self.assertEqual(self.client.bucket.uploads, 2)
        self.assertEqual(self.client.bucket.downloads, 0)

    def test_cancel_edit_requires_confirmation_and_keeps_saved_values(self):
        repo = RequestsRepository(self.client, USER_A)
        row, _ = repo.create({"numero_volo": "SALVATO123"},
                             {"pdf_ricevuta": SimpleNamespace(pdf=b"%PDF-test")}, str(uuid4()))
        self.app.run()
        self.goto_history()
        self.click("Modifica / aggiungi carta d'imbarco")
        self.app.text_input(key="edit_field_numero_volo").set_value("BOZZA456").run()
        self.uploads["edit_boarding"] = image_upload("blue")
        self.app.run()
        self.assertTrue(self.app.button(key="edit_reset").disabled)
        self.app.checkbox(key="edit_confirm_reset").check().run()
        self.assertEqual(self.app.text_input(key="edit_field_numero_volo").value, "BOZZA456")
        self.uploads.clear()
        self.app.button(key="edit_reset").click().run()
        self.assert_clean()
        self.assertNotIn("editing", self.app.session_state)
        self.assertNotIn("edit_prepared_pdf_imbarco", self.app.session_state)
        self.assertEqual(self.client.rows[0]["numero_volo"], "SALVATO123")
        self.assertIn(row["pdf_ricevuta"], self.client.bucket.files)

    def test_history_exposes_itinerary_date_and_document_state(self):
        repo = RequestsRepository(self.client, USER_A)
        repo.create({"aeroporto_partenza": "Catania [CTA]", "aeroporto_destinazione": "Roma (FCO)",
                     "data_volo": "2026-11-17", "costo_tratta": "123.45"},
                    {"pdf_ricevuta": SimpleNamespace(pdf=b"%PDF-test")}, str(uuid4()))
        self.app.run()
        self.goto_history()
        self.assert_clean()
        label = self.app.expander[0].label
        self.assertIn("Catania \\[CTA\\] → Roma \\(FCO\\)", label)
        self.assertIn("17/11/2026", label)
        self.assertIn("Documenti da completare", label)
        self.assertTrue(any("L'app non calcola rimborsi e non invia domande" in item.value for item in self.app.caption))
        self.assertEqual(self.app.metric[0].value, "€ 123,45")
        self.assertTrue(any("1 richiesta salvata." in item.value for item in self.app.caption))
        self.assertEqual(self.client.bucket.downloads, 0)

    def test_history_pagination_preserves_on_demand_download(self):
        repo = RequestsRepository(self.client, USER_A)
        for number in range(21):
            repo.create({"numero_volo": f"DEMO{number}"},
                        {"pdf_ricevuta": SimpleNamespace(pdf=b"%PDF-test")}, str(uuid4()))
        self.app.run()
        self.goto_history()
        self.assertEqual(len(self.app.expander), 20)
        self.assertTrue(next(b for b in self.app.button if b.label == "Pagina precedente").disabled)
        self.click("Pagina successiva")
        self.assert_clean()
        self.assertEqual(self.app.session_state["history_page"], 1)
        self.assertEqual(len(self.app.expander), 1)
        self.assertTrue(next(b for b in self.app.button if b.label == "Pagina successiva").disabled)
        self.click("Pagina precedente")
        self.assert_clean()
        self.assertEqual(len(self.app.expander), 20)
        self.assertEqual(self.client.bucket.downloads, 0)

    def test_login_remains_available_with_native_labeled_controls(self):
        del self.app.session_state["user_id"]
        credentials = []
        self.client.auth.sign_in_with_password = lambda value: (
            credentials.append(value) or SimpleNamespace(user=self.client.user, session=object()))
        self.app.run()
        self.assertEqual([tab.label for tab in self.app.tabs], ["Accedi", "Registrati"])
        self.assertEqual([item.label for item in self.app.text_input[:2]], ["Email", "Password"])
        self.app.text_input[0].set_value("persona@example.invalid")
        self.app.text_input[1].set_value("password-inventata-123")
        self.click("Accedi")
        self.assert_clean()
        self.assertEqual(credentials, [{"email": "persona@example.invalid", "password": "password-inventata-123"}])
        self.assertEqual(self.app.session_state["user_id"], USER_A)

    def test_registration_preserves_minimum_password_validation(self):
        del self.app.session_state["user_id"]
        self.app.run()
        self.app.text_input(key="registration_email").set_value("persona@example.invalid")
        self.app.text_input(key="registration_password").set_value("breve")
        self.click("Crea account")
        self.assertEqual(list(self.app.exception), [])
        self.assertTrue(any("almeno 12 caratteri" in item.value for item in self.app.error))
        self.assertNotIn("user_id", self.app.session_state)

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
        with patch("rimborso_voli.extraction.extract", return_value={"numero_volo": "OLD123"}), \
                patch("rimborso_voli.ocr_fallback.extract_local") as ocr:
            self.click("Leggi i nuovi documenti con AI")
        ocr.assert_not_called()
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "OLD123")
        self.app.button(key="new_remove_pdf_ricevuta").click().run()
        self.uploads["new_receipt_1"] = image_upload("red")
        self.app.run()
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "")

    def test_groq_success_does_not_call_ocr_or_save(self):
        self.add_receipt()
        with patch("rimborso_voli.extraction.extract", return_value={"numero_volo": "GRO777"}), \
                patch("rimborso_voli.ocr_fallback.extract_local") as ocr:
            self.click("Leggi i nuovi documenti con AI")
        ocr.assert_not_called()
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "GRO777")
        self.assertTrue(any("Groq" in success.value for success in self.app.success))
        self.assertEqual(self.client.rows, [])

    def test_failed_ai_still_allows_manual_saving(self):
        self.add_receipt_with_manual_value()
        error = groq_status_error(503, "secret-provider-details")
        with patch("rimborso_voli.extraction.extract", side_effect=error), \
                patch("rimborso_voli.ocr_fallback.extract_local", return_value={}):
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
        with patch("rimborso_voli.extraction.extract") as api, \
                patch("rimborso_voli.ocr_fallback.extract_local", return_value={"numero_volo": "OCR123"}):
            self.click("Leggi i nuovi documenti con AI")
        api.assert_not_called()
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "OCR123")
        self.assertTrue(any("non è configurata" in warning.value for warning in self.app.warning))
        self.assertTrue(any("OCR locale" in warning.value for warning in self.app.warning))

    def test_quota_error_is_a_distinct_category(self):
        self.add_receipt()
        error = groq_status_error(429, "quota-exhausted-secret-marker")
        with patch("rimborso_voli.extraction.extract", side_effect=error), \
                patch("rimborso_voli.ocr_fallback.extract_local", return_value={"numero_volo": "OCRQ1"}):
            self.click("Leggi i nuovi documenti con AI")
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "OCRQ1")
        self.assertTrue(any("Quota o limite del servizio" in warning.value for warning in self.app.warning))
        self.assertFalse(any(warning.value == "Groq è temporaneamente non disponibile: i dati sono stati letti con l'OCR locale. Riprova più tardi."
                             for warning in self.app.warning))

    def test_503_with_failed_ocr_keeps_both_diagnoses(self):
        self.add_receipt_with_manual_value()
        error = groq_status_error(503, "provider-raw-details")
        with patch("rimborso_voli.extraction.extract", side_effect=error), \
                patch("rimborso_voli.ocr_fallback.extract_local", side_effect=RuntimeError("ocr broken")):
            self.click("Leggi i nuovi documenti con AI")
        self.assertEqual(list(self.app.exception), [])
        rendered = " ".join(item.value for item in self.app.error)
        self.assertIn("temporaneamente non disponibile", rendered)
        self.assertIn("Anche l'OCR locale non è riuscito", rendered)
        self.assert_manual_fields_preserved()

    def test_network_error_with_failed_ocr_keeps_reason(self):
        self.add_receipt_with_manual_value()
        with patch("rimborso_voli.extraction.extract", side_effect=groq_connection_error()), \
                patch("rimborso_voli.ocr_fallback.extract_local", side_effect=RuntimeError("ocr broken")):
            self.click("Leggi i nuovi documenti con AI")
        self.assertEqual(list(self.app.exception), [])
        rendered = " ".join(item.value for item in self.app.error)
        self.assertIn("non è raggiungibile", rendered)
        self.assertIn("Anche l'OCR locale non è riuscito", rendered)
        self.assert_manual_fields_preserved()

    def test_non_string_groq_model_falls_back_to_default(self):
        self.add_receipt()
        self.app.secrets["GROQ_MODEL"] = 12345
        captured = {}
        with patch("rimborso_voli.extraction.extract") as api:
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
        from rimborso_voli import app as app_module
        with patch("rimborso_voli.app.st", SimpleNamespace(secrets=secrets)):
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
                patch("rimborso_voli.ocr_fallback.extract_local") as ocr, \
                patch("rimborso_voli.extraction.extract", side_effect=RuntimeError("no api call expected")):
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
        with patch("rimborso_voli.extraction.extract", side_effect=error), \
                patch("rimborso_voli.ocr_fallback.extract_local") as ocr:
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
        with patch("rimborso_voli.extraction.extract", side_effect=error), \
                patch("rimborso_voli.ocr_fallback.extract_local") as ocr:
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
                with patch("rimborso_voli.extraction.extract", side_effect=error), \
                        patch("rimborso_voli.ocr_fallback.extract_local") as ocr:
                    self.click("Leggi i nuovi documenti con AI")
                ocr.assert_not_called()
                self.assertEqual(list(self.app.exception), [])
                rendered = " ".join(item.value for item in self.app.error)
                self.assertIn("configurazione o autorizzazione da verificare", rendered)
                self.assertNotIn("bad request marker", rendered)
                self.assert_manual_fields_preserved()

    def test_oversized_input_blocks_analysis_without_ocr_or_call(self):
        self.add_receipt_with_manual_value()
        with patch("rimborso_voli.extraction.extract", side_effect=extraction.InputNotSendable()), \
                patch("rimborso_voli.ocr_fallback.extract_local") as ocr:
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
        with patch("rimborso_voli.extraction.extract", side_effect=extraction.ConfigurationError()), \
                patch("rimborso_voli.ocr_fallback.extract_local") as ocr:
            self.click("Leggi i nuovi documenti con AI")
        ocr.assert_not_called()
        self.assertEqual(list(self.app.exception), [])
        rendered = " ".join(item.value for item in self.app.error)
        self.assertIn("configurazione o autorizzazione da verificare", rendered)
        self.assert_manual_fields_preserved()

    def test_network_error_falls_back_to_ocr(self):
        self.add_receipt()
        with patch("rimborso_voli.extraction.extract", side_effect=groq_connection_error()), \
                patch("rimborso_voli.ocr_fallback.extract_local", return_value={"numero_volo": "OCR456"}):
            self.click("Leggi i nuovi documenti con AI")
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "OCR456")
        self.assertTrue(any("non è raggiungibile" in warning.value or "Impossibile raggiungere" in warning.value
                            for warning in self.app.warning))

    def test_invalid_groq_response_falls_back_to_ocr(self):
        self.add_receipt()
        error = ValidationError("risposta non valida")
        with patch("rimborso_voli.extraction.extract", side_effect=error), \
                patch("rimborso_voli.ocr_fallback.extract_local", return_value={"numero_volo": "OCR000"}):
            self.click("Leggi i nuovi documenti con AI")
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "OCR000")
        self.assertTrue(any("dati validi" in warning.value for warning in self.app.warning))

    def test_provider_details_never_reach_ui_or_log(self):
        self.add_receipt()
        error = groq_status_error(500, "raw-500-provider-secret-marker")
        with patch("rimborso_voli.extraction.extract", side_effect=error), \
                patch("rimborso_voli.ocr_fallback.extract_local", return_value={"numero_volo": "OCRX1"}), \
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
        self.add_receipt()
        buttons = [button.label for button in self.app.button]
        self.assertIn("Esci", buttons)
        self.app.session_state["download"] = (1, "pdf_ricevuta", b"private")
        self.app.session_state["new_field_numero_volo"] = "BOZZA123"
        self.app.session_state["new_extracted_values"] = {"numero_volo": "AI123"}
        self.app.session_state["edit_prepared_pdf_imbarco"] = self.app.session_state["new_prepared_pdf_ricevuta"]
        self.app.session_state["edit_upload_error_pdf_ricevuta"] = "Errore sintetico"
        self.click("Esci")
        self.assertNotIn("user_id", self.app.session_state)
        self.assertNotIn("download", self.app.session_state)
        self.assertNotIn("new_field_numero_volo", self.app.session_state)
        self.assertNotIn("new_extracted_values", self.app.session_state)
        self.assertNotIn("new_prepared_pdf_ricevuta", self.app.session_state)
        self.assertNotIn("edit_prepared_pdf_imbarco", self.app.session_state)
        self.assertNotIn("edit_upload_error_pdf_ricevuta", self.app.session_state)
        self.assertEqual(list(self.app.exception), [])


if __name__ == "__main__":
    unittest.main()
