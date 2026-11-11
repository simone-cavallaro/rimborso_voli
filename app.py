import time
from hashlib import sha256
from uuid import uuid4

import groq
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from domain import (
    FIELDS, MAX_UPLOAD_BYTES, ValidationError, display_amount, display_date,
    normalize_fields, plain_label, prepare_document,
)
from extraction import DEFAULT_MODEL, ConfigurationError, InputNotSendable, extract
import ocr_fallback
from repository import ConflictError, RequestsRepository, UncertainWriteError
from security import clear_session, log_failure, session_client


LABELS = {
    "compagnia_aerea": "Compagnia", "numero_volo": "Numero volo",
    "aeroporto_partenza": "Aeroporto di partenza", "aeroporto_destinazione": "Aeroporto di destinazione",
    "data_acquisto": "Data di acquisto (gg/mm/aaaa)", "data_volo": "Data del volo (gg/mm/aaaa)",
    "costo_tratta": "Costo della singola tratta (€)",
}
TEMPORARY_CODES = (408, 500, 502, 503, 504)
# Only the categories with a documented OCR fallback explain it to the user.
# 429 is a category of its own: a service quota or rate limit may have been
# reached, so it is never reported as a generic "temporarily unavailable".
OCR_FALLBACK_MESSAGES = {
    "not_configured": "L'analisi AI (Groq) non è configurata: i dati sono stati letti con l'OCR locale.",
    "temporary": "Groq è temporaneamente non disponibile: i dati sono stati letti con l'OCR locale. Riprova più tardi.",
    "quota": "Quota o limite del servizio di analisi raggiunto: i dati sono stati letti con l'OCR locale. Riprova più tardi.",
    "network": "Impossibile raggiungere il servizio di analisi: i dati sono stati letti con l'OCR locale. Controlla la connessione e riprova.",
    "response": "Il servizio di analisi non ha restituito dati validi: i dati sono stati letti con l'OCR locale.",
}
UNCERTAIN_OCR_MESSAGES = {
    "not_configured": "L'analisi AI non è configurata e l'OCR locale non ha riconosciuto dati utilizzabili: compila i campi manualmente.",
    "temporary": "L'analisi AI è temporaneamente non disponibile e l'OCR locale non ha riconosciuto dati utilizzabili: nessun dato è stato salvato. Riprova più tardi o compila i campi manualmente.",
    "quota": "L'analisi AI ha raggiunto una quota o un limite del servizio e l'OCR locale non ha riconosciuto dati utilizzabili: nessun dato è stato salvato. Riprova più tardi o compila i campi manualmente.",
    "network": "Il servizio di analisi non è raggiungibile e l'OCR locale non ha riconosciuto dati utilizzabili: nessun dato è stato salvato. Controlla la connessione e compila i campi manualmente.",
    "response": "Il servizio di analisi non ha restituito dati validi e l'OCR locale non ha riconosciuto dati utilizzabili: compila i campi manualmente.",
}
# Permanent errors never trigger the OCR fallback: the application states the
# outcome in fixed, safe words, because an HTTP status alone cannot prove which
# specific configuration detail failed.
PERMANENT_MESSAGES = {
    "configuration": "Analisi AI non eseguita: configurazione o autorizzazione da verificare. Compila i campi manualmente o riprova più tardi.",
    "model": "Analisi AI non eseguita: modello o configurazione non accessibili da verificare. Compila i campi manualmente o riprova più tardi.",
    "unclassified": "Analisi AI non riuscita (errore non classificato). Compila i campi manualmente o riprova più tardi.",
    "malformed_configuration": "Analisi AI non eseguita: impostazioni non leggibili (configurazione malformata). Verifica le impostazioni indicate nel README, poi riprova.",
    "input_not_sendable": "Analisi AI non eseguita: i documenti sono ancora troppo grandi per l'invio. Riduci le dimensioni delle immagini o compila i campi manualmente.",
}

# Streamlit 1.63.0: StreamlitSecretNotFoundError extends FileNotFoundError and
# carries a stable telemetry identifier. Structured ids verified in the locked
# streamlit.runtime.secrets module:
# - "no-secrets-found": no secrets file or directory found at all (absence);
# - "failed-parsing-secrets-file" / "invalid-secrets-path" /
#   "secrets-directory-entry-not-folder": the configured secrets source is
#   present but unreadable or malformed. Any other id stays malformed: fail
#   safe, without exposing the exception text.
_MALFORMED_SECRETS_IDS = (
    "failed-parsing-secrets-file",
    "invalid-secrets-path",
    "secrets-directory-entry-not-folder",
)


def groq_settings():
    """Return (api_key, model, mode) from Streamlit secrets.

    mode is "configured", "absent" or "malformed". Absence keeps the
    documented OCR fallback; a malformed secrets source is a blocking
    configuration outcome with a fixed message and never leaks exception
    text into the application.
    """
    try:
        api_key = st.secrets.get("GROQ_API_KEY")
        model = st.secrets.get("GROQ_MODEL")
    except StreamlitSecretNotFoundError as exc:
        error_id = getattr(exc, "error_id", None)
        if error_id != "no-secrets-found":
            log_failure("groq_settings", exc)
            return None, DEFAULT_MODEL, "malformed"
        api_key, model = None, None
    except FileNotFoundError:
        api_key, model = None, None
    api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
    model = model.strip() if isinstance(model, str) and model.strip() else DEFAULT_MODEL
    return api_key, model, ("configured" if api_key else "absent")


def api_error_reason(exc):
    """Map an APIStatusError to a fixed, safe category (no diagnosis beyond the code)."""
    code = getattr(exc, "status_code", None)
    if code == 429:
        return "quota"
    if code in TEMPORARY_CODES:
        return "temporary"
    if code == 404:
        return "model"
    if code in (400, 401, 403):
        return "configuration"
    return "unclassified"


def fail(operation, exc):
    log_failure(operation, exc)
    if isinstance(exc, (ValidationError, ConflictError, UncertainWriteError)):
        st.error(str(exc))
    else:
        st.error("Operazione non riuscita. I dati potrebbero non essere aggiornati: controlla lo storico prima di riprovare.")


def fail_extraction(exc):
    log_failure("extract_documents", exc)
    code = getattr(exc, "status_code", None)
    if isinstance(exc, ValidationError):
        st.error(str(exc))
    elif isinstance(exc, groq.APIStatusError) and (code in TEMPORARY_CODES or code == 429):
        st.error("Il servizio di analisi è temporaneamente non disponibile. Nessun dato è stato salvato: riprova più tardi o compila i campi manualmente.")
    else:
        st.error("L'analisi AI non è riuscita. Nessun dato è stato salvato: puoi riprovare o compilare i campi manualmente.")


def reset_editor(prefix):
    for key in list(st.session_state):
        if key.startswith(prefix + "_"):
            del st.session_state[key]


def flash(message, warnings=()):
    st.session_state["notice"] = (message, list(warnings))


def show_notice():
    notice = st.session_state.pop("notice", None)
    if notice:
        st.success(notice[0])
        for warning in notice[1]:
            st.warning(warning)


def authenticate(client):
    if "user_id" in st.session_state:
        try:
            session = client.auth.get_session()
            response = client.auth.get_user() if session else None
            if not response or not response.user or str(response.user.id) != st.session_state["user_id"]:
                raise ValueError("Session expired")
            return response.user
        except Exception as exc:
            log_failure("verify_session", exc)
            clear_session(st.session_state)
            flash("La sessione è terminata. Accedi nuovamente.")
            st.rerun()

    login, registration = st.tabs(["Accedi", "Registrati"])
    with login:
        with st.form("login"):
            email = st.text_input("Email", max_chars=254)
            password = st.text_input("Password", type="password", max_chars=128)
            submitted = st.form_submit_button("Accedi")
        if submitted:
            try:
                response = client.auth.sign_in_with_password({"email": email.strip(), "password": password})
                if not response.user or not response.session:
                    raise ValueError("No authenticated session")
                clear_session(st.session_state)
                st.session_state["supabase_client"] = client
                st.session_state["user_id"] = str(response.user.id)
            except Exception as exc:
                log_failure("login", exc)
                st.error("Accesso non riuscito. Verifica le credenziali e la conferma dell'email.")
            else:
                st.rerun()
    with registration:
        with st.form("registration"):
            email = st.text_input("Email", key="registration_email", max_chars=254)
            password = st.text_input("Password", key="registration_password", type="password", max_chars=128,
                                     help="Usa almeno 12 caratteri e una password diversa da altri servizi.")
            submitted = st.form_submit_button("Crea account")
        if submitted:
            if len(password) < 12:
                st.error("La password deve contenere almeno 12 caratteri.")
            else:
                try:
                    result = client.auth.sign_up({"email": email.strip(), "password": password})
                    if result.session:
                        client.auth.sign_out({"scope": "local"})
                    clear_session(st.session_state)
                    flash("Se la registrazione può essere completata, riceverai un'email con le istruzioni. Poi accedi.")
                except Exception as exc:
                    log_failure("registration", exc)
                    st.error("Registrazione non riuscita. Riprova più tardi.")
                else:
                    st.rerun()
    st.stop()


def uploads(prefix, editing=False):
    # One uploader per row: on a phone the two columns were cramped and hard to tap.
    receipt = st.file_uploader("Sostituisci ricevuta (facoltativo)" if editing else "Ricevuta di pagamento",
                               type=["jpg", "jpeg", "png"], key=f"{prefix}_receipt", max_upload_size=10)
    boarding = st.file_uploader("Aggiungi o sostituisci carta d'imbarco (facoltativo)",
                                type=["jpg", "jpeg", "png"], key=f"{prefix}_boarding", max_upload_size=10)
    documents, fingerprints = {}, []
    valid = True
    for field, uploaded in (("pdf_ricevuta", receipt), ("pdf_imbarco", boarding)):
        if uploaded is None:
            fingerprints.append(None)
            st.session_state.pop(f"{prefix}_prepared_{field}", None)
            continue
        try:
            if uploaded.size > MAX_UPLOAD_BYTES:
                raise ValidationError("Ogni immagine deve essere di massimo 10 MB.")
            data = uploaded.getvalue()
            digest = sha256(data).hexdigest()
            fingerprints.append(digest)
            key = f"{prefix}_prepared_{field}"
            cached = st.session_state.get(key)
            if not cached or cached.digest != digest:
                st.session_state[key] = prepare_document(data)
            documents[field] = st.session_state[key]
        except Exception as exc:
            valid = False
            fingerprints.append("invalid")
            fail("validate_upload", exc)
    return documents, tuple(fingerprints), valid


def set_editor_values(prefix, values):
    for field in FIELDS:
        value = values.get(field)
        if field.startswith("data_"):
            value = display_date(value)
        elif field == "costo_tratta":
            value = display_amount(value)
        st.session_state[f"{prefix}_field_{field}"] = value or ""


def analyze_documents(documents):
    """Run Groq, then OCR only in the documented fallback cases.

    Returns (values, source, reason):
    - ("groq", values, None): Groq succeeded.
    - ("ocr", values, reason): a documented Groq failure triggered the OCR.
    - ("ocr_failed", None, reason): same as above, but the OCR failed too; the
      Groq reason is preserved so the diagnosis is never lost.
    - ("blocked", None, reason): a permanent request/configuration/model error
      or an input that cannot be sent; no OCR fallback is attempted and no
      automatic diagnosis is invented.

    Unexpected exceptions (bugs) propagate; only the documented Groq failure
    modes are handled.
    """
    api_key, model, mode = groq_settings()
    if mode == "malformed":
        return None, "blocked", "malformed_configuration"
    if not api_key:
        try:
            return ocr_fallback.extract_local(documents), "ocr", "not_configured"
        except Exception as exc:
            log_failure("ocr_fallback", exc)
            return None, "ocr_failed", "not_configured"
    reason = None
    try:
        return extract(documents, api_key, model), "groq", None
    except groq.APIStatusError as exc:
        reason = api_error_reason(exc)
        log_failure("extract_documents", exc)
        if reason in PERMANENT_MESSAGES:
            return None, "blocked", reason
    except groq.APIConnectionError as exc:
        reason = "network"
        log_failure("extract_documents", exc)
    except ConfigurationError as exc:
        # Local configuration problems (for example refused custom headers)
        # are not provider errors: block the analysis with the fixed message.
        log_failure("extract_documents", exc)
        return None, "blocked", "configuration"
    except InputNotSendable as exc:
        # Oversized input never reaches the network: it must not cause an
        # OCR fallback with a fake "invalid response" reason.
        log_failure("extract_documents", exc)
        return None, "blocked", "input_not_sendable"
    except ValidationError as exc:
        # A 200 response that is not valid JSON/fields is still a Groq
        # failure: the local OCR can provide a best-effort fallback. This is
        # documented in the analysis notice and in the README.
        reason = "response"
        log_failure("extract_documents", exc)
    try:
        return ocr_fallback.extract_local(documents), "ocr", reason
    except Exception as exc:
        log_failure("ocr_fallback", exc)
        return None, "ocr_failed", reason


def editor(prefix, initial, documents, changed, valid):
    if changed:
        set_editor_values(prefix, initial)
    if documents:
        with st.expander("Anteprima dei nuovi documenti"):
            for field, document in documents.items():
                st.image(document.image, caption="Ricevuta" if field == "pdf_ricevuta" else "Carta d'imbarco",
                         width="stretch")
    st.caption("L'analisi automatica è facoltativa. Solo premendo il pulsante, le immagini selezionate vengono inviate a Groq, se configurato. Se Groq non è configurato, temporaneamente non disponibile, vincolato da quota o limite, non raggiungibile o restituisce dati non validi, l'app prova l'OCR locale. In caso di errori di configurazione, autorizzazione o modello l'analisi si interrompe, ma resta la compilazione manuale. Verifica sempre i dati proposti, soprattutto il costo della singola tratta.")
    if st.button("Leggi i nuovi documenti con AI", key=f"{prefix}_analyze", type="primary",
                 width="stretch", disabled=not documents or not valid):
        try:
            if time.monotonic() - st.session_state.get("last_analysis", -100) < 15:
                raise ValidationError("Attendi qualche secondo prima di ripetere l'analisi.")
            st.session_state["last_analysis"] = time.monotonic()
            with st.spinner("Lettura dei documenti..."):
                values, source, reason = analyze_documents(documents)
            current = {field: st.session_state.get(f"{prefix}_field_{field}", initial.get(field)) for field in FIELDS}
            if values:
                merged = {**current, **{k: v for k, v in values.items() if v not in (None, "")}}
                set_editor_values(prefix, merged)
            if source == "groq":
                st.success("Analisi completata con Groq. Controlla e correggi i dati prima di salvare.")
            elif source == "ocr":
                message = OCR_FALLBACK_MESSAGES.get(
                    reason, "L'analisi Groq non è riuscita: i dati sono stati letti con l'OCR locale. Controlla i campi con attenzione.")
                if not any(value not in (None, "") for value in values.values()):
                    message += " L'OCR locale non ha riconosciuto dati sufficientemente affidabili: compila i campi manualmente."
                st.warning(message)
            elif source == "ocr_failed":
                # Both engines failed: the Groq reason and the OCR outcome
                # stay separated so no diagnosis is lost.
                st.error(UNCERTAIN_OCR_MESSAGES.get(reason, PERMANENT_MESSAGES["unclassified"]))
                st.error("Anche l'OCR locale non è riuscito: nessun dato è stato salvato. Compila i campi manualmente.")
            else:
                st.error(PERMANENT_MESSAGES.get(reason, PERMANENT_MESSAGES["unclassified"]))
        except Exception as exc:
            fail_extraction(exc)
    st.caption("Puoi lasciare vuoti i dati che non conosci e completarli in seguito. Le date e gli importi inseriti devono essere validi.")
    with st.form(f"{prefix}_form"):
        values = {}
        for field in FIELDS:
            values[field] = st.text_input(LABELS[field], key=f"{prefix}_field_{field}", max_chars=160)
        save = st.form_submit_button("Salva modifiche" if prefix == "edit" else "Salva richiesta",
                                     type="primary", width="stretch", disabled=not valid)
    return normalize_fields(values) if save else None


def new_request(repo):
    st.subheader("Nuova richiesta")
    st.write("Salva la ricevuta appena acquisti il volo. Potrai aggiungere la carta d'imbarco dallo storico prima della partenza.")
    if st.button("Svuota e inizia una nuova richiesta", width="stretch"):
        reset_editor("new")
        st.rerun()
    documents, fingerprint, valid = uploads("new")
    changed = st.session_state.get("new_fingerprint") != fingerprint
    if changed or "new_request_id" not in st.session_state:
        st.session_state["new_fingerprint"] = fingerprint
        st.session_state["new_request_id"] = str(uuid4())
    try:
        values = editor("new", {}, documents, changed, valid)
        if values is not None:
            with st.spinner("Salvataggio della richiesta..."):
                _, warnings = repo.create(values, documents, st.session_state["new_request_id"])
            reset_editor("new")
            flash("Richiesta salvata. La trovi nello storico, dove puoi completarla o modificarla.", warnings)
            st.rerun()
    except Exception as exc:
        fail("create_request", exc)


def edit_request(repo):
    record = st.session_state["editing"]
    st.subheader("Modifica richiesta")
    st.write("I documenti già salvati vengono conservati. Carica soltanto quelli da aggiungere o sostituire.")
    st.info("Ricevuta salvata" if record.get("pdf_ricevuta") else "Ricevuta da aggiungere")
    st.info("Carta d'imbarco salvata" if record.get("pdf_imbarco") else "Carta d'imbarco da aggiungere")
    if st.button("Annulla e torna allo storico", width="stretch"):
        st.session_state.pop("editing", None)
        reset_editor("edit")
        st.rerun()
    documents, fingerprint, valid = uploads("edit", editing=True)
    changed = st.session_state.get("edit_fingerprint") != fingerprint
    st.session_state["edit_fingerprint"] = fingerprint
    try:
        values = editor("edit", record, documents, changed, valid)
        if values is not None:
            with st.spinner("Salvataggio delle modifiche..."):
                _, warnings = repo.update(record, values, documents)
            st.session_state.pop("editing", None)
            st.session_state.pop("download", None)
            reset_editor("edit")
            flash("Richiesta aggiornata.", warnings)
            st.rerun()
    except Exception as exc:
        fail("update_request", exc)


def history(repo):
    if "editing" in st.session_state:
        edit_request(repo)
        return
    st.subheader("Storico rimborsi")
    page = st.session_state.get("history_page", 0)
    try:
        records, has_more = repo.page(page)
        summary = repo.summary()
    except Exception as exc:
        fail("load_history", exc)
        st.info("Se il problema persiste, contatta l'amministratore della piattaforma.")
        return
    total = display_amount(summary["totale_speso"]) or "0,00"
    st.metric("Totale spese aeree", f"€ {total}")
    st.caption(f"{summary['numero_richieste']} richieste complessive. Il totale include solo gli importi validi già inseriti.")
    if not records:
        st.info("Nessuna richiesta in questa pagina.")
    st.caption(f"Pagina {page + 1} · fino a 20 richieste per pagina")
    for row in records:
        record_id = row["id"]
        status = "Documenti completi" if row.get("pdf_imbarco") and row.get("pdf_ricevuta") else "Documenti da completare"
        title = f"{display_date(row.get('data_volo')) or 'Data da inserire'} · {row.get('compagnia_aerea') or 'Compagnia da inserire'} {row.get('numero_volo') or ''} · {status}"
        with st.expander(plain_label(title)):
            st.text(f"{row.get('aeroporto_partenza') or '—'} → {row.get('aeroporto_destinazione') or '—'}")
            st.text(f"Acquisto: {display_date(row.get('data_acquisto')) or 'Da inserire'}")
            amount = display_amount(row.get("costo_tratta"))
            st.text(f"Costo tratta: {amount + ' €' if amount else 'Da inserire'}")
            if not row.get("pdf_imbarco"):
                st.info("Puoi aggiungere la carta d'imbarco quando sarà disponibile.")
            if st.button("Modifica / aggiungi carta d'imbarco", key=f"edit_button_{record_id}",
                         width="stretch"):
                reset_editor("edit")
                st.session_state["editing"] = row
                st.session_state.pop("download", None)
                st.rerun()
            for field, label in (("pdf_ricevuta", "Ricevuta"), ("pdf_imbarco", "Carta d'imbarco")):
                if row.get(field) and st.button(f"Prepara download: {label}", key=f"prepare_{field}_{record_id}",
                                                width="stretch"):
                    try:
                        data = repo.download(record_id, field)
                        st.session_state["download"] = (record_id, field, data)
                    except Exception as exc:
                        fail("download_document", exc)
                cached = st.session_state.get("download")
                if cached and cached[:2] == (record_id, field):
                    st.download_button(f"Scarica {label}", cached[2], file_name=f"{field}_{record_id}.pdf",
                                       mime="application/pdf", key=f"download_{field}_{record_id}",
                                       on_click="ignore", width="stretch")
            confirm = st.checkbox("Confermo di voler eliminare questa richiesta", key=f"confirm_{record_id}")
            if st.button("Elimina richiesta", key=f"delete_{record_id}", width="stretch",
                         disabled=not confirm):
                try:
                    warnings = repo.delete(row)
                    st.session_state.pop("download", None)
                    flash("Richiesta eliminata.", warnings)
                    st.rerun()
                except Exception as exc:
                    fail("delete_request", exc)
    previous, following = st.columns(2)
    if previous.button("Pagina precedente", width="stretch", disabled=page == 0):
        st.session_state["history_page"] = page - 1
        st.session_state.pop("download", None)
        st.rerun()
    if following.button("Pagina successiva", width="stretch", disabled=not has_more):
        st.session_state["history_page"] = page + 1
        st.session_state.pop("download", None)
        st.rerun()


def main():
    st.set_page_config(page_title="Caro Voli Sicilia", page_icon="✈️",
                       layout="centered", initial_sidebar_state="auto")
    st.title("✈️ Caro Voli Sicilia")
    show_notice()
    try:
        client = session_client(st.session_state, st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])
    except Exception as exc:
        log_failure("configuration", exc)
        st.error("La piattaforma non è configurata correttamente. Contatta l'amministratore.")
        st.stop()
    user = authenticate(client)
    api_key, _model, mode = groq_settings()
    if mode == "malformed":
        st.caption("Analisi AI: impostazioni non leggibili (configurazione malformata). Correggi le impostazioni indicate nel README; fino ad allora l'analisi resta disabilitata.")
    elif api_key:
        st.caption("Analisi AI: Groq configurato. Connessione e chiave vengono verificati solo quando avvii un'analisi.")
    else:
        st.caption("Analisi AI: non configurata. Con le immagini caricate puoi usare l'OCR locale, se il server lo fornisce.")
    account, logout = st.columns([3, 1])
    account.caption(f"Accesso: {user.email}")
    if logout.button("Esci", key="logout", width="stretch"):
        try:
            client.auth.sign_out({"scope": "local"})
        except Exception as exc:
            log_failure("logout", exc)
        finally:
            clear_session(st.session_state)
        st.rerun()
    repo = RequestsRepository(client, user.id)
    view = st.radio("Vai a", ["Nuova richiesta", "Storico rimborsi"], horizontal=True, key="view")
    if view == "Nuova richiesta":
        new_request(repo)
    else:
        history(repo)


if __name__ == "__main__":
    main()
