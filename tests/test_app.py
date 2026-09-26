from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
from streamlit.testing.v1 import AppTest

from repository import RequestsRepository
from tests.fakes import FakeClient, USER_A

ROOT = Path(__file__).resolve().parents[1]


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
        self.app.secrets.update(SUPABASE_URL="https://example.supabase.co", SUPABASE_KEY="sb_publishable_test", GEMINI_API_KEY="test")
        self.app.session_state["user_id"] = USER_A

    def assert_clean(self):
        self.assertEqual(list(self.app.exception), [])
        self.assertEqual(list(self.app.error), [])

    def click(self, label):
        next(button for button in self.app.button if button.label == label).click().run()

    def test_receipt_only_create_then_boarding_added_from_history(self):
        self.uploads["new_receipt"] = image_upload()
        self.app.run()
        self.assert_clean()
        self.app.text_input(key="new_field_numero_volo").set_value("AB123")
        self.click("Salva richiesta")
        self.assert_clean()
        self.assertEqual(len(self.client.rows), 1)
        receipt = self.client.rows[0]["pdf_ricevuta"]
        self.assertIsNone(self.client.rows[0]["pdf_imbarco"])

        self.app.sidebar.radio[0].set_value("Storico rimborsi").run()
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
        self.uploads["new_receipt"] = image_upload()
        self.app.run()
        with patch("extraction.extract", return_value={"numero_volo": "OLD123"}):
            self.click("Leggi i nuovi documenti con AI")
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "OLD123")
        self.uploads["new_receipt"] = image_upload("red")
        self.app.run()
        self.assert_clean()
        self.assertEqual(self.app.text_input(key="new_field_numero_volo").value, "")

    def test_failed_ai_still_allows_manual_saving(self):
        self.uploads["new_receipt"] = image_upload()
        self.app.run()
        with patch("extraction.extract", side_effect=RuntimeError("secret-provider-details")):
            self.click("Leggi i nuovi documenti con AI")
        self.assertNotIn("secret-provider-details", str(self.app.error))
        self.app.text_input(key="new_field_numero_volo").set_value("AB789")
        self.click("Salva richiesta")
        self.assert_clean()
        self.assertEqual(self.client.rows[0]["numero_volo"], "AB789")

    def test_invalid_cost_blocks_save_without_upload(self):
        self.uploads["new_receipt"] = image_upload()
        self.app.run()
        self.app.text_input(key="new_field_costo_tratta").set_value("NaN")
        self.click("Salva richiesta")
        self.assertTrue(self.app.error)
        self.assertEqual(self.client.rows, [])
        self.assertEqual(self.client.bucket.uploads, 0)

    def test_download_on_demand_and_delete_confirmation(self):
        repo = RequestsRepository(self.client, USER_A)
        row, _ = repo.create({}, {"pdf_ricevuta": SimpleNamespace(pdf=b"%PDF-test")}, str(uuid4()))
        self.app.run()
        self.app.sidebar.radio[0].set_value("Storico rimborsi").run()
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

    def test_logout_clears_sensitive_state(self):
        self.app.run()
        self.app.session_state["download"] = (1, "pdf_ricevuta", b"private")
        self.click("Esci")
        self.assertNotIn("user_id", self.app.session_state)
        self.assertNotIn("download", self.app.session_state)
        self.assertEqual(list(self.app.exception), [])


if __name__ == "__main__":
    unittest.main()
