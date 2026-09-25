import time
from hashlib import sha256
from uuid import uuid4

import streamlit as st

from domain import (
    FIELDS, MAX_UPLOAD_BYTES, ValidationError, display_amount, display_date,
    normalize_fields, plain_label, prepare_document,
)
from extraction import extract
from repository import ConflictError, RequestsRepository, UncertainWriteError
from security import clear_session, log_failure, session_client


LABELS = {
    "compagnia_aerea": "Compagnia", "numero_volo": "Numero volo",
    "aeroporto_partenza": "Aeroporto di partenza", "aeroporto_destinazione": "Aeroporto di destinazione",
    "data_acquisto": "Data di acquisto (gg/mm/aaaa)", "data_volo": "Data del volo (gg/mm/aaaa)",
    "costo_tratta": "Costo della singola tratta (€)",
}


def fail(operation, exc):
    log_failure(operation, exc)
    if isinstance(exc, (ValidationError, ConflictError, UncertainWriteError)):
        st.error(str(exc))
    else:
        st.error("Operazione non riuscita. I dati potrebbero non essere aggiornati: controlla lo storico prima di riprovare.")


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
    left, right = st.columns(2)
    with left:
        receipt = st.file_uploader("Sostituisci ricevuta (facoltativo)" if editing else "Ricevuta di pagamento",
                                   type=["jpg", "jpeg", "png"], key=f"{prefix}_receipt", max_upload_size=10)
    with right:
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


def editor(prefix, initial, documents, changed, valid):
    if changed:
        set_editor_values(prefix, initial)
    if documents:
        with st.expander("Anteprima dei nuovi documenti"):
            for field, document in documents.items():
                st.image(document.image, caption="Ricevuta" if field == "pdf_ricevuta" else "Carta d'imbarco", width=400)
    st.caption("L'analisi AI è facoltativa. Solo premendo il pulsante, le immagini selezionate vengono inviate a Google Gemini. Verifica sempre i dati estratti, soprattutto il costo della singola tratta.")
    if st.button("Leggi i nuovi documenti con AI", key=f"{prefix}_analyze", disabled=not documents or not valid):
        try:
            if time.monotonic() - st.session_state.get("last_analysis", -100) < 15:
                raise ValidationError("Attendi qualche secondo prima di ripetere l'analisi.")
            st.session_state["last_analysis"] = time.monotonic()
            set_editor_values(prefix, initial)
            with st.spinner("Lettura dei documenti..."):
                values = extract(documents, st.secrets["GEMINI_API_KEY"],
                                 st.secrets.get("GEMINI_MODEL", "models/gemini-3.6-flash"))
            merged = {**initial, **{k: v for k, v in values.items() if v not in (None, "")}}
            set_editor_values(prefix, merged)
            st.success("Analisi completata. Controlla e correggi i dati prima di salvare.")
        except Exception as exc:
            fail("extract_documents", exc)
    st.caption("Puoi lasciare vuoti i dati che non conosci e completarli in seguito. Le date e gli importi inseriti devono essere validi.")
    with st.form(f"{prefix}_form"):
        values = {}
        for field in FIELDS:
            values[field] = st.text_input(LABELS[field], key=f"{prefix}_field_{field}", max_chars=160)
        save = st.form_submit_button("Salva modifiche" if prefix == "edit" else "Salva richiesta", disabled=not valid)
    return normalize_fields(values) if save else None


def new_request(repo):
    st.subheader("Nuova richiesta")
    st.write("Salva la ricevuta appena acquisti il volo. Potrai aggiungere la carta d'imbarco dallo storico prima della partenza.")
    if st.button("Svuota e inizia una nuova richiesta"):
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
    if st.button("Annulla e torna allo storico"):
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
            if st.button("Modifica / aggiungi carta d'imbarco", key=f"edit_button_{record_id}"):
                reset_editor("edit")
                st.session_state["editing"] = row
                st.session_state.pop("download", None)
                st.rerun()
            for field, label in (("pdf_ricevuta", "Ricevuta"), ("pdf_imbarco", "Carta d'imbarco")):
                if row.get(field) and st.button(f"Prepara download: {label}", key=f"prepare_{field}_{record_id}"):
                    try:
                        data = repo.download(record_id, field)
                        st.session_state["download"] = (record_id, field, data)
                    except Exception as exc:
                        fail("download_document", exc)
                cached = st.session_state.get("download")
                if cached and cached[:2] == (record_id, field):
                    st.download_button(f"Scarica {label}", cached[2], file_name=f"{field}_{record_id}.pdf",
                                       mime="application/pdf", key=f"download_{field}_{record_id}", on_click="ignore")
            confirm = st.checkbox("Confermo di voler eliminare questa richiesta", key=f"confirm_{record_id}")
            if st.button("Elimina richiesta", key=f"delete_{record_id}", disabled=not confirm):
                try:
                    warnings = repo.delete(row)
                    st.session_state.pop("download", None)
                    flash("Richiesta eliminata.", warnings)
                    st.rerun()
                except Exception as exc:
                    fail("delete_request", exc)
    previous, following = st.columns(2)
    if previous.button("Pagina precedente", disabled=page == 0):
        st.session_state["history_page"] = page - 1
        st.session_state.pop("download", None)
        st.rerun()
    if following.button("Pagina successiva", disabled=not has_more):
        st.session_state["history_page"] = page + 1
        st.session_state.pop("download", None)
        st.rerun()


def main():
    st.set_page_config(page_title="Caro Voli Sicilia", page_icon="✈️")
    st.title("✈️ Caro Voli Sicilia")
    show_notice()
    try:
        client = session_client(st.session_state, st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])
    except Exception as exc:
        log_failure("configuration", exc)
        st.error("La piattaforma non è configurata correttamente. Contatta l'amministratore.")
        st.stop()
    user = authenticate(client)
    st.sidebar.text(f"Accesso: {user.email}")
    if st.sidebar.button("Esci"):
        try:
            client.auth.sign_out({"scope": "local"})
        except Exception as exc:
            log_failure("logout", exc)
        finally:
            clear_session(st.session_state)
        st.rerun()
    repo = RequestsRepository(client, user.id)
    view = st.sidebar.radio("Vai a", ["Nuova richiesta", "Storico rimborsi"])
    if view == "Nuova richiesta":
        new_request(repo)
    else:
        history(repo)


if __name__ == "__main__":
    main()
