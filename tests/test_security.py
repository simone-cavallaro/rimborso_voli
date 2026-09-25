import base64
import json
import unittest
from unittest.mock import patch

from security import clear_session, session_client, validate_public_key


def jwt_key(role):
    payload = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class SecurityTests(unittest.TestCase):
    def test_reject_privileged_and_malformed_keys(self):
        for key in ("sb_secret_example", jwt_key("service_role"), "invalid", "header.invalid.sig"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_public_key(key)
        validate_public_key(jwt_key("anon"))
        validate_public_key("sb_publishable_example")

    def test_distinct_clients_and_auth_storage_per_session(self):
        first, second = {}, {}
        with patch("security.create_client", side_effect=lambda *args, **kwargs: object()) as factory:
            a = session_client(first, "https://example.supabase.co", jwt_key("anon"))
            b = session_client(second, "https://example.supabase.co", jwt_key("anon"))
            self.assertIsNot(a, b)
            self.assertIs(a, session_client(first, "https://example.supabase.co", jwt_key("anon")))
            self.assertIsNot(factory.call_args_list[0].kwargs["options"].storage,
                             factory.call_args_list[1].kwargs["options"].storage)

    def test_logout_clears_documents_and_identity(self):
        state = {"user_id": "a", "new_field_numero_volo": "AB123", "download": b"document", "supabase_client": object()}
        clear_session(state)
        self.assertEqual(state, {})

    def test_require_https(self):
        with self.assertRaises(ValueError):
            session_client({}, "http://example.supabase.co", jwt_key("anon"))


if __name__ == "__main__":
    unittest.main()
