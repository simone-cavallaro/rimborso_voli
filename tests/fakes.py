"""In-memory transport for failure injection; deliberately does NOT enforce RLS."""

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"


class FakeQuery:
    def __init__(self, client):
        self.client = client
        self.action = "select"
        self.filters = []
        self.payload = None
        self.slice = slice(None)
        self.orders = []

    def select(self, *args, **kwargs):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def limit(self, n):
        self.slice = slice(0, n)
        return self

    def range(self, start, end):
        self.slice = slice(start, end + 1)
        return self

    def order(self, key, desc=False):
        self.orders.append((key, desc))
        return self

    def insert(self, payload):
        self.action, self.payload = "insert", deepcopy(payload)
        return self

    def update(self, payload):
        self.action, self.payload = "update", deepcopy(payload)
        return self

    def delete(self):
        self.action = "delete"
        return self

    def execute(self):
        self.client.calls.append((self.action, list(self.filters)))
        self.client.maybe_fail(self.action, "before")
        rows = [row for row in self.client.rows if all(row.get(k) == v for k, v in self.filters)]
        if self.action == "insert":
            if any(r["client_request_id"] == self.payload["client_request_id"] for r in self.client.rows):
                raise RuntimeError("unique constraint")
            row = {"id": str(uuid4()), "version": 1, "created_at": "2026-09-25T12:00:00Z", **self.payload}
            self.client.rows.append(row)
            rows = [row]
        elif self.action == "update":
            for row in rows:
                row.update(self.payload)
                row["version"] += 1
        elif self.action == "delete":
            self.client.rows = [row for row in self.client.rows if row not in rows]
        else:
            for key, desc in reversed(self.orders):
                rows.sort(key=lambda row: row[key], reverse=desc)
            rows = rows[self.slice]
        self.client.maybe_fail(self.action, "after")
        return SimpleNamespace(data=deepcopy(rows))


class FakeBucket:
    def __init__(self):
        self.files = {}
        self.uploads = 0
        self.fail_upload_at = None
        self.fail_remove = False
        self.downloads = 0

    def upload(self, *, path, file, file_options):
        self.uploads += 1
        if self.uploads == self.fail_upload_at:
            raise ConnectionError("upload failure")
        assert file_options["upsert"] == "false"
        assert path not in self.files
        self.files[path] = file

    def remove(self, paths):
        if self.fail_remove:
            raise ConnectionError("remove failure")
        for path in paths:
            self.files.pop(path, None)

    def download(self, path):
        self.downloads += 1
        return self.files[path]


class FakeClient:
    def __init__(self, user_id=USER_A):
        self.rows = []
        self.calls = []
        self.failures = []
        self.bucket = FakeBucket()
        self.storage = SimpleNamespace(from_=lambda name: self.bucket)
        self.user = SimpleNamespace(id=user_id, email="test@example.invalid")
        self.auth = SimpleNamespace(
            get_session=lambda: object(),
            get_user=lambda: SimpleNamespace(user=self.user),
            sign_out=lambda *args: None,
        )

    def table(self, name):
        assert name == "richieste"
        return FakeQuery(self)

    def rpc(self, name, params):
        assert name == "richieste_summary"
        rows = [r for r in self.rows if r["user_id"] == self.user.id]
        total = sum((Decimal(r["costo_tratta"]) for r in rows if r.get("costo_tratta")), Decimal("0"))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data={
            "totale_speso": str(total), "numero_richieste": len(rows),
        }))

    def maybe_fail(self, action, stage):
        if self.failures and self.failures[0] == (action, stage):
            self.failures.pop(0)
            raise ConnectionError(f"simulated {action} {stage}")
