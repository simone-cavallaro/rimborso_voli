"""Session-local authentication. No credentials or personal data in error logs."""

import base64
import json
import logging
from urllib.parse import urlparse

from supabase import create_client
from supabase.lib.client_options import SyncClientOptions
from supabase_auth import SyncMemoryStorage


def validate_public_key(key):
    if not isinstance(key, str) or not key:
        raise ValueError("Configure a Supabase publishable/anon key.")
    if key.startswith("sb_publishable_"):
        return
    if key.startswith("sb_secret_"):
        raise ValueError("Privileged Supabase keys are not allowed.")
    try:
        payload = key.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if claims.get("role") != "anon":
            raise ValueError("Only an anon key is allowed.")
    except (IndexError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Configure a Supabase publishable/anon key.") from exc
    # This only guards configuration mistakes. Supabase verifies the actual JWT.


def session_client(state, url, key):
    validate_public_key(key)
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Supabase requires an HTTPS URL without credentials.")
    if "supabase_client" not in state:
        state["supabase_client"] = create_client(
            url, key,
            options=SyncClientOptions(
                storage=SyncMemoryStorage(), auto_refresh_token=False,
                postgrest_client_timeout=30, storage_client_timeout=30,
            ),
        )
    return state["supabase_client"]


def clear_session(state):
    for key in list(state):
        del state[key]


def log_failure(operation, exc):
    # Do not log exception text/tracebacks: providers may include tokens or documents.
    logging.getLogger("rimborso_voli").warning("%s failed (%s)", operation, type(exc).__name__)
