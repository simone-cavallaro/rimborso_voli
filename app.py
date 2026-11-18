import time
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

    st.write("Conserva le ricevute dei voli e aggiungi la carta d'imbarco quando sarà disponibile.")
    st.caption("La domanda di rimborso si presenta sul portale Caro Voli Sicilia: l'app non la invia e non ne calcola l'importo.")
    login, registration = st.tabs(["Accedi", "Registrati"])
    with login:
        with st.form("login"):
            email = st.text_input("Email", max_chars=254)
            password = st.text_input("Password", type="password", max_chars=128)
            submitted = st.form_submit_button("Accedi", type="primary", width="stretch")
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
            submitted = st.form_submit_button("Crea account", type="primary", width="stretch")
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


def mark_upload_changed(prefix, field):
    # Only an uploader event means explicit selection/removal, not view cleanup.
    st.session_state[f"{prefix}_upload_changed_{field}"] = True


def remove_draft_document(prefix, field, slot):
    for kind in ("prepared", "upload_error", "upload_changed"):
        st.session_state.pop(f"{prefix}_{kind}_{field}", None)
    generation = f"{prefix}_{slot}_generation"
    # A new widget identity clears the selector without assigning its value.
    st.session_state[generation] = st.session_state.get(generation, 0) + 1


def uploads(prefix, editing=False):
    st.subheader("Documenti")
    st.caption("Immagini JPG, JPEG o PNG · massimo 10 MB per file. La carta d'imbarco è facoltativa.")
    st.caption("I documenti della bozza restano solo in questa sessione, anche cambiando vista. Salva la richiesta per conservarli nello storico.")
    if editing:
        st.caption("Rimuovere un nuovo documento dalla bozza non cancella quello già salvato.")
    documents, fingerprints = {}, []
    valid = True
    for field, slot, label, upload_label in (
        ("pdf_ricevuta", "receipt", "Ricevuta", "Sostituisci ricevuta (facoltativo)" if editing else "Ricevuta di pagamento"),
        ("pdf_imbarco", "boarding", "Carta d'imbarco", "Aggiungi o sostituisci carta d'imbarco (facoltativo)"),
    ):
        key = f"{prefix}_prepared_{field}"
        error_key = f"{prefix}_upload_error_{field}"
        # A retained document has no active selector: replacement starts with removal.
        if key not in st.session_state:
            generation = st.session_state.get(f"{prefix}_{slot}_generation", 0)
            widget_key = f"{prefix}_{slot}" + (f"_{generation}" if generation else "")
            uploaded = st.file_uploader(upload_label, type=["jpg", "jpeg", "png"], key=widget_key,
                                        max_upload_size=10, on_change=mark_upload_changed, args=(prefix, field))
            changed = st.session_state.pop(f"{prefix}_upload_changed_{field}", False)
            if uploaded is None and changed:
                st.session_state.pop(error_key, None)
            elif uploaded is not None:
                try:
                    if uploaded.size > MAX_UPLOAD_BYTES:
                        raise ValidationError("Ogni immagine deve essere di massimo 10 MB.")
                    data = uploaded.getvalue()
                    st.session_state[key] = prepare_document(data)
                    st.session_state.pop(error_key, None)
                except Exception as exc:
                    log_failure("validate_upload", exc)
                    st.session_state[error_key] = str(exc) if isinstance(exc, ValidationError) else "Immagine non valida. Rimuovila o scegli un altro file JPG o PNG."
                else:
                    # Immediately retire the accepted selector, before another choice.
                    st.rerun()
        cached = st.session_state.get(key)
        error = st.session_state.get(error_key)
        if error:
            valid = False
            fingerprints.append("invalid")
            st.error(error)
        elif cached:
            documents[field] = cached
            fingerprints.append(cached.digest)
            st.text(f"{label}: conservata nella bozza (solo questa sessione).")
            st.caption("Per scegliere un altro file, rimuovi prima questo documento dalla bozza.")
        else:
            fingerprints.append(None)
        if cached or error:
            st.button(f"Rimuovi {label.lower()} dalla bozza", key=f"{prefix}_remove_{field}",
                      on_click=remove_draft_document, args=(prefix, field, slot), width="stretch")
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
        previous_ai = st.session_state.pop(f"{prefix}_extracted_values", {})
        retained = {
            field: st.session_state[f"{prefix}_field_{field}"] for field in FIELDS
            if f"{prefix}_field_{field}" in st.session_state
            and (field not in previous_ai or st.session_state[f"{prefix}_field_{field}"] != previous_ai[field])
        }
        set_editor_values(prefix, initial)
        for field, value in retained.items():
            st.session_state[f"{prefix}_field_{field}"] = value
    if documents:
        with st.expander("Anteprima dei nuovi documenti"):
            for field, document in documents.items():
                st.image(document.image, caption="Ricevuta" if field == "pdf_ricevuta" else "Carta d'imbarco",
                         width="stretch")
    st.caption("Analisi facoltativa: solo premendo il pulsante invii le immagini selezionate a Groq, se configurato. Puoi anche compilare i dati manualmente. Controlla sempre il risultato.")
    with st.expander("Dettagli e disponibilità dell'analisi"):
        api_key, _model, mode = groq_settings()
        if mode == "malformed":
            st.caption("Analisi AI: impostazioni non leggibili (configurazione malformata). Correggi le impostazioni indicate nel README; fino ad allora l'analisi resta disabilitata.")
        elif api_key:
            st.caption("Analisi AI: Groq configurato. Connessione e chiave vengono verificati solo quando avvii un'analisi.")
        else:
            st.caption("Analisi AI: non configurata. Con le immagini caricate puoi usare l'OCR locale, se il server lo fornisce.")
        st.caption("Se Groq non è configurato, temporaneamente non disponibile, vincolato da quota o limite, non raggiungibile o restituisce dati non validi, l'app prova l'OCR locale. In caso di errori di configurazione, autorizzazione o modello l'analisi si interrompe, ma resta la compilazione manuale. Verifica sempre i dati proposti, soprattutto il costo della singola tratta.")
    if st.button("Leggi i nuovi documenti con AI", key=f"{prefix}_analyze",
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
                previous_ai = st.session_state.get(f"{prefix}_extracted_values", {})
                st.session_state[f"{prefix}_extracted_values"] = {
                    **previous_ai,
                    **{field: st.session_state[f"{prefix}_field_{field}"]
                       for field, value in values.items() if value not in (None, "")},
                }
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
    st.subheader("Dati del volo")
    st.caption("Lascia vuoti i dati che non conosci: potrai completarli in seguito. Date in gg/mm/aaaa e importi come 123,45.")
    # Native inputs commit each change, including before an upload or navigation.
    with st.container(border=True):
        values = {}
        for heading, fields in (
            ("Itinerario", ("aeroporto_partenza", "aeroporto_destinazione", "compagnia_aerea", "numero_volo")),
            ("Date e spesa", ("data_volo", "data_acquisto", "costo_tratta")),
        ):
            st.markdown(f"**{heading}**")
            for field in fields:
                values[field] = st.text_input(LABELS[field], key=f"{prefix}_field_{field}", max_chars=160)
        st.caption("Il costo riguarda la singola tratta. Salvare qui non invia una domanda di rimborso.")
        save = st.button("Salva modifiche" if prefix == "edit" else "Salva richiesta",
                         key=f"{prefix}_save", type="primary", width="stretch", disabled=not valid)
    return normalize_fields(values) if save else None


def discard_draft(prefix, editing=False):
    with st.expander("Annulla modifiche" if editing else "Svuota bozza"):
        st.caption("I dati e i nuovi documenti non salvati andranno persi. Le richieste e i documenti già salvati restano nello storico.")
        confirm = st.checkbox("Confermo di voler scartare la bozza", key=f"{prefix}_confirm_reset")
        label = "Annulla e torna allo storico" if editing else "Svuota e inizia una nuova richiesta"
        if st.button(label, key=f"{prefix}_reset", width="stretch", disabled=not confirm):
            reset_editor(prefix)
            if editing:
                st.session_state.pop("editing", None)
            st.rerun()


def new_request(repo):
    st.subheader("Nuova richiesta", divider="yellow")
    st.write("Salva la ricevuta appena acquisti il volo. Potrai aggiungere la carta d'imbarco dallo storico prima della partenza.")
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
    discard_draft("new")


def edit_request(repo):
    record = st.session_state["editing"]
    st.subheader("Modifica richiesta", divider="yellow")
    st.text(f"{record.get('aeroporto_partenza') or 'Partenza da inserire'} → {record.get('aeroporto_destinazione') or 'Destinazione da inserire'}")
    st.write("I documenti già salvati vengono conservati. Carica soltanto quelli da aggiungere o sostituire.")
    st.badge("Ricevuta salvata" if record.get("pdf_ricevuta") else "Ricevuta da aggiungere",
             color="blue" if record.get("pdf_ricevuta") else "yellow")
    st.badge("Carta d'imbarco salvata" if record.get("pdf_imbarco") else "Carta d'imbarco da aggiungere",
             color="blue" if record.get("pdf_imbarco") else "yellow")
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
    discard_draft("edit", editing=True)


def open_new_request():
    # A callback runs before the navigation radio is instantiated again.
    st.session_state["view"] = "Nuova richiesta"


def history(repo):
    if "editing" in st.session_state:
        edit_request(repo)
        return
    st.subheader("Storico richieste", divider="yellow")
    page = st.session_state.get("history_page", 0)
    try:
        records, has_more = repo.page(page)
        summary = repo.summary()
    except Exception as exc:
        fail("load_history", exc)
        st.info("Se il problema persiste, contatta l'amministratore della piattaforma.")
        return
    total = display_amount(summary["totale_speso"]) or "0,00"
    with st.container(border=True):
        st.metric("Totale spese aeree", f"€ {total}")
        count = summary["numero_richieste"]
        requests_label = "1 richiesta salvata" if count == 1 else f"{count} richieste salvate"
        st.caption(f"{requests_label}. Il totale include solo gli importi validi già inseriti.")
    st.caption("Le spese non sono il rimborso spettante. L'app non calcola rimborsi e non invia domande. «Documenti completi» indica solo la presenza di ricevuta e carta d'imbarco.")
    if not records:
        st.info("Non hai ancora salvato richieste. Inizia dalla ricevuta del tuo volo." if not summary["numero_richieste"] else "Nessuna richiesta in questa pagina.")
        st.button("Crea una nuova richiesta", key="history_create", type="primary",
                  width="stretch", on_click=open_new_request)
    st.caption(f"Pagina {page + 1} · fino a 20 richieste per pagina")
    for row in records:
        record_id = row["id"]
        status = "Documenti completi" if row.get("pdf_imbarco") and row.get("pdf_ricevuta") else "Documenti da completare"
        itinerary = f"{row.get('aeroporto_partenza') or 'Partenza da inserire'} → {row.get('aeroporto_destinazione') or 'Destinazione da inserire'}"
        title = f"{itinerary} · {display_date(row.get('data_volo')) or 'Data da inserire'} · {status}"
        with st.expander(plain_label(title)):
            st.text(itinerary)
            st.text(f"Volo: {row.get('compagnia_aerea') or 'Compagnia da inserire'} {row.get('numero_volo') or ''}")
            st.badge(status, color="blue" if row.get("pdf_imbarco") and row.get("pdf_ricevuta") else "yellow")
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
            st.caption("Eliminazione definitiva: verranno rimossi la richiesta e i suoi documenti.")
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
    with st.container(horizontal=True):
        if st.button("Pagina precedente", width="stretch", disabled=page == 0):
            st.session_state["history_page"] = page - 1
            st.session_state.pop("download", None)
            st.rerun()
        if st.button("Pagina successiva", width="stretch", disabled=not has_more):
            st.session_state["history_page"] = page + 1
            st.session_state.pop("download", None)
            st.rerun()


def main():
    st.set_page_config(page_title="Caro Voli Sicilia", page_icon="✈️",
                       layout="centered", initial_sidebar_state="auto")
    st.title("Caro Voli Sicilia")
    show_notice()
    try:
        client = session_client(st.session_state, st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])
    except Exception as exc:
        log_failure("configuration", exc)
        st.error("La piattaforma non è configurata correttamente. Contatta l'amministratore.")
        st.stop()
    user = authenticate(client)
    # Keep text drafts when Streamlit removes widgets from the inactive view.
    for prefix in ("new", "edit"):
        for field in FIELDS:
            key = f"{prefix}_field_{field}"
            if key in st.session_state:
                st.session_state[key] = st.session_state[key]
    st.text(f"Accesso: {user.email}", wrap=True)
    if st.button("Esci", key="logout"):
        try:
            client.auth.sign_out({"scope": "local"})
        except Exception as exc:
            log_failure("logout", exc)
        finally:
            clear_session(st.session_state)
        st.rerun()
    repo = RequestsRepository(client, user.id)
    view = st.radio("Vai a", ["Nuova richiesta", "Storico richieste"], horizontal=True, key="view")
    if view == "Nuova richiesta":
        new_request(repo)
    else:
        history(repo)


if __name__ == "__main__":
    main()
