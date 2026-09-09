# ✈️ Caro Voli Sicilia: richiedere i rimborsi non è mai stato così semplice!

Un'applicazione web full-stack sviluppata con **Streamlit** e integrata con modelli LLM multimodali per automatizzare e semplificare il processo di richiesta rimborso voli per i residenti in Sicilia.

## 🌐 Utilizza l'App Online (Consigliato)
La piattaforma è completamente deployata e accessibile in cloud. Non è richiesta alcuna installazione.              
**https://rimborso-voli-sicilia.streamlit.app/**

*Nota: L'applicazione richiede la registrazione di un account gratuito. Grazie all'architettura multi-tenant, tutti i dati e i documenti caricati sono strettamente privati e visibili solo al proprietario.*



## Il Problema
I residenti in Sicilia hanno diritto a un rimborso statale (fino al 50%) sui voli nazionali. Tuttavia, il portale della Pubblica Amministrazione richiede l'inserimento manuale di numerosi dati (PNR, numero volo, date, aeroporti, costi) e l'upload esclusivo in formato PDF di carte d'imbarco e ricevute. Questo processo ripetitivo e mensile genera un inutile spreco di tempo.

## La Soluzione
Questa piattaforma risolve il problema automatizzando l'intero processo end-to-end:
1. **Upload Semplificato:** L'utente carica le immagini (JPG/PNG) della carta d'imbarco e della ricevuta;
2. **Estrazione AI Multimodale:** Utilizzando le API vision di Gemini, il sistema incrocia i due documenti, estrae semanticamente tutti i campi richiesti e li restituisce in un JSON strutturato, ignorando la complessità e la variabilità dei layout delle diverse compagnie aeree;
3. **Conversione Automatica:** Le immagini vengono convertite istantaneamente nel formato PDF richiesto dal portale istituzionale;
4. **SaaS Multi-Tenant:** I dati e i file PDF generati vengono salvati in modo sicuro su un'infrastruttura cloud PostgreSQL (Supabase), con accesso segregato tramite Row Level Security (RLS) per ogni singolo utente autenticato.

## Strumenti utilizzati
*   **Frontend & App Logic:** [Streamlit](https://streamlit.io/) (Python)
*   **AI / NLP Vision:** Modello `gemini-3.6-flash`
*   **Database & Auth:** PostgreSQL + [Supabase](https://supabase.com/)
*   **Storage Cloud:** Supabase Buckets
*   **Conversione File:** `img2pdf`, `Pillow`
*   **Deploy:** Streamlit Community Cloud

## Architettura e Funzionalità Principali
*   **Sistema di Autenticazione:** Accesso protetto con registrazione e login utente.
*   **Human-in-the-Loop:** Interfaccia di revisione e modifica manuale dei dati estratti prima del commit sul database.
*   **Data Cleaning:** Pipeline interna per la normalizzazione dei tipi di dato (es. conversione dei formati valutari locali in standard float per il backend).
*   **Isolamento Dati (RLS):** Grazie alle policy SQL su Supabase, le tabelle e gli storage bucket permettono operazioni CRUD esclusivamente al proprietario del record.

## 💻 Come eseguire l'app localmente
Se sei uno sviluppatore e desideri far girare l'applicazione sul tuo computer:

1. **Clona la repository**

```bash
 git clone [https://github.com/il-tuo-username/rimborsi-sicilia-ai.git](https://github.com/il-tuo-username/rimborsi-sicilia-ai.git)
 cd rimborsi-sicilia-ai
```

2. **Crea un ambiente virtuale e installa le dipendenze**

```bash
uv venv
.venv\Scripts\activate  # Usa 'source .venv/bin/activate' su macOS/Linux
uv pip install -r requirements.txt
```


3. **Configura i Secrets**              
Crea una cartella `.streamlit` nella root del progetto e aggiungi un file `secrets.toml` con le tue chiavi API:

```bash
GEMINI_API_KEY = "la_tua_chiave_google_ai" # la crei in maniera gratuita sul sito "Google AI studio"
SUPABASE_URL = "il_tuo_url_supabase"
SUPABASE_KEY = "la_tua_chiave_supabase"
```

4. **Avvia l'app**

```bash
streamlit run app.py
```