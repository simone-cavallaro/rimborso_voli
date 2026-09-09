import streamlit as st
import google.generativeai as genai
import img2pdf
from PIL import Image
import json
import pandas as pd
from supabase import create_client, Client

# --- INIZIALIZZAZIONE SUPABASE ---
@st.cache_resource
def init_supabase() -> Client:
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

supabase = init_supabase()
BUCKET_NAME = "pdf_rimborsi"

st.title("✈️ Caro Voli Sicilia: Richiedere i rimborsi non è mai stato così semplice!")

# ==========================================
# GESTIONE AUTENTICAZIONE (LOGIN / REGISTRAZIONE)
# ==========================================
if 'user' not in st.session_state:
    st.markdown("### Accesso alla Piattaforma")
    tab_login, tab_reg = st.tabs(["🔑 Login", "📝 Registrati"])
    
    with tab_login:
        with st.form("login_form"):
            email_log = st.text_input("Email")
            pass_log = st.text_input("Password", type="password")
            if st.form_submit_button("Accedi"):
                try:
                    res = supabase.auth.sign_in_with_password({"email": email_log, "password": pass_log})
                    st.session_state['user'] = res.user
                    st.rerun()
                except Exception as e:
                    st.error("Credenziali non valide.")
                    
    with tab_reg:
        with st.form("reg_form"):
            email_reg = st.text_input("Email")
            pass_reg = st.text_input("Password", type="password", help="Minimo 6 caratteri")
            if st.form_submit_button("Crea Account"):
                try:
                    res = supabase.auth.sign_up({"email": email_reg, "password": pass_reg})
                    st.success("Account creato! Verifica la tua email perconferma il tuo account.")
                except Exception as e:
                    st.error(f"Errore: {e}")
                    
    st.stop() # Blocca l'esecuzione del resto dell'app se non si è loggati

# Se siamo qui, l'utente è loggato.
user = st.session_state['user']
st.sidebar.success(f"Loggato come: {user.email}")
if st.sidebar.button("Esci"):
    supabase.auth.sign_out()
    del st.session_state['user']
    st.rerun()

# --- FUNZIONE DI CANCELLAZIONE ---
def delete_richiesta(record_id, path_imbarco, path_ricevuta):
    if path_imbarco:
        supabase.storage.from_(BUCKET_NAME).remove([path_imbarco])
    if path_ricevuta:
        supabase.storage.from_(BUCKET_NAME).remove([path_ricevuta])
    supabase.table("richieste").delete().eq("id", record_id).execute()
    st.rerun()

# --- CONFIGURAZIONE LLM ---
genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
model = genai.GenerativeModel('models/gemini-3.6-flash', generation_config={"response_mime_type": "application/json"})

# ==========================================
# APPLICATIVO PRINCIPALE
# ==========================================
tab1, tab2 = st.tabs(["➕ Nuova Richiesta", "🗂️ Storico Rimborsi"])

with tab1:
    st.markdown("### 1. Caricamento Documenti")
    col1, col2 = st.columns(2)
    file_imbarco = col1.file_uploader("🎫 Carta d'Imbarco", type=["jpg", "jpeg", "png"])
    file_ricevuta = col2.file_uploader("🧾 Ricevuta", type=["jpg", "jpeg", "png"])

    if file_imbarco and file_ricevuta:
        col_img1, col_img2 = st.columns(2)
        img_imbarco = Image.open(file_imbarco)
        img_ricevuta = Image.open(file_ricevuta)
        col_img1.image(img_imbarco, use_container_width=True)
        col_img2.image(img_ricevuta, use_container_width=True)
        
        if st.button("Analizza Documenti"):
            with st.spinner("Incrocio dei dati in corso..."):
                prompt = """Estrai i seguenti dati in formato JSON valido, date in dd/mm/yyyy. Chiavi: "numero_volo", "data_acquisto", "data_volo", "aeroporto_partenza", "aeroporto_destinazione", "compagnia_aerea", "costo_tratta"."""
                try:
                    response = model.generate_content([prompt, img_imbarco, img_ricevuta])
                    st.session_state['dati'] = json.loads(response.text)
                    st.success("Estrazione completata!")
                except Exception as e:
                    st.error("Errore durante l'analisi.")

        if 'dati' in st.session_state:
            with st.form("form_revisione"):
                c1, c2 = st.columns(2)
                compagnia = c1.text_input("Compagnia", value=st.session_state['dati'].get('compagnia_aerea', ''))
                volo = c1.text_input("Volo", value=st.session_state['dati'].get('numero_volo', ''))
                partenza = c1.text_input("Da", value=st.session_state['dati'].get('aeroporto_partenza', ''))
                destinazione = c1.text_input("A", value=st.session_state['dati'].get('aeroporto_destinazione', ''))
                
                data_acquisto = c2.text_input("Acquisto", value=st.session_state['dati'].get('data_acquisto', ''))
                data_volo = c2.text_input("Data Volo", value=st.session_state['dati'].get('data_volo', ''))
                costo = c2.number_input("Costo Tratta", value=float(st.session_state['dati'].get('costo_tratta', 0.0)))

                if st.form_submit_button("Salva in Cloud"):
                    with st.spinner("Salvataggio..."):
                        pdf_imb = img2pdf.convert(file_imbarco.getvalue())
                        pdf_ric = img2pdf.convert(file_ricevuta.getvalue())
                        
                        nome_base = f"{compagnia.replace(' ', '')}_{volo.replace(' ', '')}_{data_volo.replace('/', '-')}"
                        
                        # Salviamo i file isolandoli nella cartella col nome dell'ID utente!
                        path_imbarco = f"{user.id}/Imbarco_{nome_base}.pdf"
                        path_ricevuta = f"{user.id}/Ricevuta_{nome_base}.pdf"
                        
                        supabase.storage.from_(BUCKET_NAME).upload(file=pdf_imb, path=path_imbarco, file_options={"content-type": "application/pdf"})
                        supabase.storage.from_(BUCKET_NAME).upload(file=pdf_ric, path=path_ricevuta, file_options={"content-type": "application/pdf"})
                        
                        nuovo_record = {
                            "user_id": user.id, # Assegniamo il biglietto al proprietario
                            "numero_volo": volo, "data_acquisto": data_acquisto, "data_volo": data_volo,
                            "aeroporto_partenza": partenza, "aeroporto_destinazione": destinazione,
                            "compagnia_aerea": compagnia, "costo_tratta": costo,
                            "pdf_imbarco": path_imbarco, "pdf_ricevuta": path_ricevuta
                        }
                        supabase.table("richieste").insert(nuovo_record).execute()
                        del st.session_state['dati']
                        st.success("✅ Salvato nel tuo spazio personale!")

with tab2:
    st.markdown("### 🗂️ I tuoi Rimborsi Personali")
    
    # La query chiederà TUTTI i dati, ma il database restituirà SOLO quelli dell'utente loggato
    # grazie alle regole SQL inserite prima!
    res = supabase.table("richieste").select("*").order("created_at", desc=True).execute()
    
    if not res.data:
        st.info("Nessun rimborso presente nel tuo storico.")
    else:
        df = pd.DataFrame(res.data)
        st.metric(label="Totale Spese Aeree", value=f"€ {df['costo_tratta'].sum():.2f}")
        
        for _, row in df.iterrows():
            with st.expander(f"{row['data_volo']} | {row['compagnia_aerea']} {row['numero_volo']} - €{row['costo_tratta']}"):
                col_d, col_a = st.columns([2, 1])
                col_d.write(f"**{row['aeroporto_partenza']} ➔ {row['aeroporto_destinazione']}**")
                
                try:
                    imb = supabase.storage.from_(BUCKET_NAME).download(row['pdf_imbarco'])
                    ric = supabase.storage.from_(BUCKET_NAME).download(row['pdf_ricevuta'])
                    col_a.download_button("📄 Imbarco", data=imb, file_name="Imbarco.pdf", key=f"i_{row['id']}")
                    col_a.download_button("📄 Ricevuta", data=ric, file_name="Ricevuta.pdf", key=f"r_{row['id']}")
                except:
                    st.error("Errore recupero PDF")
                
                if st.button("🗑️ Elimina", key=f"d_{row['id']}", type="primary"):
                    delete_richiesta(row['id'], row['pdf_imbarco'], row['pdf_ricevuta'])