from copy import deepcopy
from types import SimpleNamespace
import unittest
from uuid import uuid4

from domain import ValidationError
from repository import ConflictError, RequestsRepository, UncertainWriteError
from tests.fakes import FakeClient, USER_A, USER_B

DOC = SimpleNamespace(pdf=b"%PDF-1.4\nsynthetic test document")


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.repo = RequestsRepository(self.client, USER_A)

    def create(self, **values):
        return self.repo.create(values, {"pdf_ricevuta": DOC}, str(uuid4()))[0]

    def test_receipt_only_then_add_boarding_and_edit_metadata(self):
        row = self.create(numero_volo="AB123")
        receipt = row["pdf_ricevuta"]
        self.assertIsNone(row["pdf_imbarco"])
        saved, warnings = self.repo.update(row, {"numero_volo": "AB456", "costo_tratta": "123,45"}, {"pdf_imbarco": DOC})
        self.assertEqual(saved["pdf_ricevuta"], receipt)
        self.assertEqual(saved["numero_volo"], "AB456")
        self.assertEqual(saved["costo_tratta"], "123.45")
        self.assertTrue(saved["pdf_imbarco"])
        self.assertEqual(saved["version"], 2)
        self.assertEqual(len(self.client.bucket.files), 2)
        self.assertEqual(warnings, [])

    def test_receipt_is_required(self):
        with self.assertRaises(ValidationError):
            self.repo.create({}, {"pdf_imbarco": DOC}, str(uuid4()))
        self.assertEqual(self.client.bucket.uploads, 0)

    def test_repeated_flight_has_unique_documents(self):
        first = self.create(numero_volo="AB123", data_volo="01/10/2026")
        second = self.create(numero_volo="AB123", data_volo="01/10/2026")
        self.assertNotEqual(first["pdf_ricevuta"], second["pdf_ricevuta"])

    def test_retry_is_idempotent(self):
        request_id = str(uuid4())
        first, _ = self.repo.create({}, {"pdf_ricevuta": DOC}, request_id)
        second, _ = self.repo.create({}, {"pdf_ricevuta": DOC}, request_id)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.client.bucket.uploads, 1)

    def test_other_user_cannot_read_update_delete_or_download(self):
        row = self.create()
        other = RequestsRepository(self.client, USER_B)
        self.assertEqual(other.page(0)[0], [])
        for call in (lambda: other.get(row["id"]), lambda: other.update(row, {}, {}),
                     lambda: other.delete(row), lambda: other.download(row["id"], "pdf_ricevuta")):
            with self.assertRaises(ConflictError):
                call()
        self.assertEqual(len(self.client.rows), 1)
        self.assertEqual(len(self.client.bucket.files), 1)

    def test_document_path_checks(self):
        for path in (f"{USER_B}/receipt.pdf", f"{USER_A}/../receipt.pdf", f"{USER_A}\\receipt.pdf", "https://example.com/a.pdf"):
            with self.assertRaises(ValidationError):
                self.repo.safe_path(path)

    def test_second_upload_failure_rolls_back_first(self):
        self.client.bucket.fail_upload_at = 2
        with self.assertRaises(ConnectionError):
            self.repo.create({}, {"pdf_ricevuta": DOC, "pdf_imbarco": DOC}, str(uuid4()))
        self.assertEqual(self.client.bucket.files, {})
        self.assertEqual(self.client.rows, [])

    def test_insert_failure_rolls_back_documents(self):
        self.client.failures = [("insert", "before")]
        with self.assertRaises(ConnectionError):
            self.create()
        self.assertEqual(self.client.bucket.files, {})

    def test_lost_insert_response_recovers_without_deleting_documents(self):
        self.client.failures = [("insert", "after")]
        row = self.create()
        self.assertIn(row["pdf_ricevuta"], self.client.bucket.files)

    def test_unknown_commit_retains_documents_for_reconciliation(self):
        self.client.failures = [("insert", "after"), ("select", "before")]
        with self.assertRaises(UncertainWriteError):
            self.create()
        self.assertEqual(len(self.client.bucket.files), 1)
        self.assertEqual(len(self.client.rows), 1)

    def test_optimistic_lock_rejects_stale_edit(self):
        original = self.create()
        self.repo.update(original, {"numero_volo": "AB001"}, {})
        with self.assertRaises(ConflictError):
            self.repo.update(original, {"numero_volo": "AB002"}, {"pdf_imbarco": DOC})
        self.assertEqual(self.client.bucket.uploads, 1)
        self.assertEqual(self.client.rows[0]["numero_volo"], "AB001")

    def test_failed_edit_preserves_old_receipt(self):
        original = self.create()
        self.client.failures = [("update", "before")]
        with self.assertRaises(ConnectionError):
            self.repo.update(original, {}, {"pdf_ricevuta": DOC})
        self.assertEqual(list(self.client.bucket.files), [original["pdf_ricevuta"]])

    def test_lost_update_response_preserves_new_receipt(self):
        original = self.create()
        self.client.failures = [("update", "after")]
        saved, _ = self.repo.update(original, {}, {"pdf_ricevuta": DOC})
        self.assertIn(saved["pdf_ricevuta"], self.client.bucket.files)
        self.assertNotIn(original["pdf_ricevuta"], self.client.bucket.files)

    def test_replace_and_delete_preserve_shared_legacy_document(self):
        original = self.create()
        duplicate = deepcopy(original)
        duplicate.update(id=str(uuid4()), client_request_id=str(uuid4()))
        self.client.rows.append(duplicate)
        replacement, _ = self.repo.update(original, {}, {"pdf_ricevuta": DOC})
        self.assertIn(original["pdf_ricevuta"], self.client.bucket.files)
        self.repo.delete(replacement)
        self.assertIn(duplicate["pdf_ricevuta"], self.client.bucket.files)
        self.repo.delete(duplicate)
        self.assertEqual(self.client.bucket.files, {})

    def test_failed_delete_keeps_documents(self):
        row = self.create()
        self.client.failures = [("delete", "before")]
        with self.assertRaises(ConnectionError):
            self.repo.delete(row)
        self.assertIn(row["pdf_ricevuta"], self.client.bucket.files)

    def test_lost_delete_response_still_cleans_up(self):
        row = self.create()
        self.client.failures = [("delete", "after")]
        self.assertEqual(self.repo.delete(row), [])
        self.assertEqual(self.client.bucket.files, {})

    def test_summary_covers_all_pages(self):
        for _ in range(22):
            self.create(costo_tratta="10")
        summary = self.repo.summary()
        self.assertEqual(summary["numero_richieste"], 22)
        self.assertEqual(summary["totale_speso"], "220.00")

    def test_cleanup_failure_reports_warning_after_successful_update(self):
        row = self.create()
        self.client.bucket.fail_remove = True
        saved, warnings = self.repo.update(row, {}, {"pdf_ricevuta": DOC})
        self.assertEqual(saved["version"], 2)
        self.assertTrue(warnings)

    def test_pagination_and_no_eager_document_download(self):
        for _ in range(22):
            self.create()
        first, more = self.repo.page(0)
        second, more_second = self.repo.page(1)
        self.assertEqual((len(first), more, len(second), more_second), (20, True, 2, False))
        self.assertEqual(self.client.bucket.downloads, 0)


if __name__ == "__main__":
    unittest.main()
