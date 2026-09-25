"""Owner-scoped persistence with immutable file names and optimistic locking."""

from uuid import UUID, uuid4

from domain import MAX_PDF_BYTES, ValidationError, normalize_fields
from security import log_failure

BUCKET = "pdf_rimborsi"
DOCUMENT_FIELDS = ("pdf_ricevuta", "pdf_imbarco")
PAGE_SIZE = 20
COLUMNS = "id,user_id,client_request_id,version,created_at,compagnia_aerea,numero_volo,data_acquisto,data_volo,aeroporto_partenza,aeroporto_destinazione,costo_tratta,pdf_ricevuta,pdf_imbarco"


class ConflictError(Exception):
    pass


class UncertainWriteError(Exception):
    pass


class RequestsRepository:
    def __init__(self, client, user_id):
        self.client = client
        self.user_id = str(UUID(str(user_id)))
        self.bucket = client.storage.from_(BUCKET)

    def owned(self):
        return self.client.table("richieste").select(COLUMNS).eq("user_id", self.user_id)

    def get(self, record_id):
        rows = self.owned().eq("id", record_id).limit(1).execute().data
        if not rows:
            raise ConflictError("Richiesta non disponibile. Aggiorna lo storico.")
        return rows[0]

    def by_request_id(self, request_id):
        rows = self.owned().eq("client_request_id", request_id).limit(1).execute().data
        return rows[0] if rows else None

    def page(self, number):
        start = max(0, int(number)) * PAGE_SIZE
        rows = self.owned().order("created_at", desc=True).order("id", desc=True).range(start, start + PAGE_SIZE).execute().data
        return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE

    def summary(self):
        # This security-invoker RPC scopes the aggregate to auth.uid() on the server.
        return self.client.rpc("richieste_summary", {}).execute().data

    def safe_path(self, path):
        if not isinstance(path, str) or not path.startswith(self.user_id + "/"):
            raise ValidationError("Il documento non appartiene a questa sessione.")
        if "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")) or not path.endswith(".pdf"):
            raise ValidationError("Percorso del documento non valido.")
        return path

    def cleanup(self, paths):
        """Never remove a document still referenced by a request (including legacy duplicates)."""
        warnings = []
        for path in set(filter(None, paths)):
            try:
                self.safe_path(path)
                referenced = any(
                    self.owned().eq(field, path).limit(1).execute().data
                    for field in DOCUMENT_FIELDS
                )
                if not referenced:
                    self.bucket.remove([path])
            except Exception as exc:
                log_failure("document_cleanup", exc)
                warnings.append("Alcuni file non più utilizzati non sono stati rimossi. Contatta l'assistenza.")
        return list(dict.fromkeys(warnings))

    def upload(self, documents, request_id, attempted):
        paths = {}
        for field, document in documents.items():
            if field not in DOCUMENT_FIELDS or document is None:
                continue
            if not document.pdf.startswith(b"%PDF-") or len(document.pdf) > MAX_PDF_BYTES:
                raise ValidationError("Documento PDF non valido o troppo grande.")
            path = f"{self.user_id}/{request_id}/{field}_{uuid4()}.pdf"
            attempted.append(path)  # Includes uploads whose response may be lost.
            self.bucket.upload(path=path, file=document.pdf, file_options={
                "content-type": "application/pdf", "upsert": "false", "cache-control": "0",
            })
            paths[field] = path
        return paths

    def create(self, values, documents, request_id):
        values = normalize_fields(values)
        request_id = str(UUID(str(request_id)))
        existing = self.by_request_id(request_id)
        if existing:
            return existing, []  # Safe retry after a lost response/double click.
        if not documents.get("pdf_ricevuta"):
            raise ValidationError("Carica la ricevuta di pagamento per salvare la richiesta.")
        attempted, write_started = [], False
        try:
            paths = self.upload(documents, request_id, attempted)
            payload = {**values, "user_id": self.user_id, "client_request_id": request_id,
                       "pdf_imbarco": None, **paths}
            write_started = True
            rows = self.client.table("richieste").insert(payload).execute().data
            if not rows:
                raise RuntimeError("Insert returned no record")
            return rows[0], []
        except Exception as exc:
            if write_started:
                try:
                    committed = self.by_request_id(request_id)
                except Exception as read_exc:
                    log_failure("create_reconciliation", read_exc)
                    raise UncertainWriteError("Salvataggio non verificabile. Controlla lo storico e riprova senza cambiare i documenti.") from exc
                if committed:
                    return committed, self.cleanup(attempted)
            self.cleanup(attempted)
            raise

    def update(self, record, values, documents):
        values = normalize_fields(values)
        current = self.get(record["id"])
        if current["version"] != record["version"]:
            raise ConflictError("La richiesta è stata modificata in un'altra sessione. Riaprila dallo storico.")
        if not current.get("pdf_ricevuta") and not documents.get("pdf_ricevuta"):
            raise ValidationError("La richiesta deve avere una ricevuta di pagamento.")
        attempted, write_started = [], False
        payload = {}
        try:
            paths = self.upload(documents, str(current["client_request_id"]), attempted)
            payload = {**values, **paths}
            write_started = True
            rows = (self.client.table("richieste").update(payload)
                    .eq("user_id", self.user_id).eq("id", record["id"])
                    .eq("version", record["version"]).execute().data)
            if not rows:
                raise ConflictError("La richiesta è cambiata durante il salvataggio. Riaprila dallo storico.")
            saved = rows[0]
        except Exception as exc:
            if write_started and not isinstance(exc, ConflictError):
                try:
                    latest = self.get(record["id"])
                except Exception as read_exc:
                    log_failure("update_reconciliation", read_exc)
                    raise UncertainWriteError("Salvataggio non verificabile. Aggiorna lo storico prima di riprovare.") from exc
                # Recover a lost response only when uploaded paths prove this write committed.
                if attempted and all(latest.get(k) == v for k, v in paths.items()):
                    saved = latest
                else:
                    self.cleanup(attempted)
                    raise
            else:
                self.cleanup(attempted)
                raise
        obsolete = [current.get(field) for field in DOCUMENT_FIELDS if field in payload]
        return saved, self.cleanup(obsolete)

    def delete(self, record):
        # Delete the row first: a DB failure must never destroy its documents.
        try:
            rows = (self.client.table("richieste").delete().eq("user_id", self.user_id)
                    .eq("id", record["id"]).eq("version", record["version"]).execute().data)
        except Exception as exc:
            try:
                remaining = self.owned().eq("id", record["id"]).limit(1).execute().data
            except Exception as read_exc:
                log_failure("delete_reconciliation", read_exc)
                raise UncertainWriteError("Eliminazione non verificabile. Aggiorna lo storico prima di riprovare.") from exc
            if remaining:
                raise
            return self.cleanup([record.get(field) for field in DOCUMENT_FIELDS])
        if not rows:
            raise ConflictError("La richiesta è cambiata. Aggiorna lo storico prima di eliminarla.")
        return self.cleanup([rows[0].get(field) for field in DOCUMENT_FIELDS])

    def download(self, record_id, field):
        if field not in DOCUMENT_FIELDS:
            raise ValidationError("Tipo di documento non valido.")
        record = self.get(record_id)
        path = record.get(field)
        if not path:
            raise ValidationError("Questo documento non è ancora stato caricato.")
        return self.bucket.download(self.safe_path(path))
