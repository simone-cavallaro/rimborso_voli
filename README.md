# ✈️ Caro Voli Sicilia: organizza i documenti per il tuo rimborso voli

Applicazione **Streamlit** per conservare ricevute e carte d'imbarco, estrarre i dati dei voli con **Groq** e preparare i PDF utili alla richiesta di rimborso sul portale Caro Voli Sicilia. La domanda resta a carico dell'utente: l'app **non invia richieste di rimborso** e non ne calcola l'importo.

È disponibile su [**rimborso-voli-sicilia.streamlit.app**](https://rimborso-voli-sicilia.streamlit.app/). Ogni utente vede solo i propri rimborsi e documenti; dati e file sono protetti da Row Level Security e da un bucket privato su Supabase.

## Come funziona

1. **Salva la ricevuta:** carica un'immagine JPG, JPEG o PNG (massimo 10 MB). La carta d'imbarco è facoltativa in questa fase.
2. **Compila i dati:** inseriscili manualmente oppure avvia l'analisi facoltativa. Al primo posto c'è Groq; l'OCR locale interviene solo se Groq non è configurato, è temporaneamente non disponibile, ha raggiunto una quota, non è raggiungibile o restituisce dati non validi. Errori di configurazione, autorizzazione o modello interrompono l'analisi e lasciano la compilazione manuale. Le immagini raggiungono Groq solo quando premi il pulsante, come copie ridotte (massimo 2048 pixel per lato, JPEG qualità 90).
3. **Controlla e correggi:** verifica sempre i dati proposti, soprattutto il costo della singola tratta per prenotazioni con più voli o passeggeri.
4. **Completa in seguito:** dallo storico puoi aggiungere la carta d'imbarco, modificare i dati o sostituire i documenti. «Documenti completi» indica solo la presenza di ricevuta e carta d'imbarco.
5. **Scarica i PDF:** l'app prepara i documenti per la compilazione sul portale.

I documenti della bozza restano nella sessione anche cambiando vista. Per sostituirne uno conservato devi prima rimuoverlo dalla bozza; un file non valido blocca salvataggio e analisi finché non viene rimosso o sostituito.

## Privacy e sicurezza

L'accesso richiede un account. L'app usa un client Supabase distinto per sessione, valida dati e immagini e non richiede chiavi privilegiate. L'analisi è facoltativa e la connessione viene verificata solo al momento dell'avvio; l'OCR gira sul server dell'app senza inviare i documenti a un'altra API. Controlla sempre il risultato prima di salvarlo.

## 💻 Esegui l'app in locale

Servono **Python 3.14** e un progetto Supabase configurato. Puoi installare le dipendenze con [`uv`](https://docs.astral.sh/uv/) o pip. La chiave Groq è facoltativa. Per l'OCR locale installa Tesseract con italiano e inglese e rendi disponibile `tesseract`; su Streamlit Cloud viene installato tramite `packages.txt`.

1. Clona il repository e installa le dipendenze:

   ```bash
   git clone https://github.com/simone-cavallaro/rimborso_voli.git
   cd rimborso_voli
   uv sync --locked
   ```

   Con pip, attiva un ambiente virtuale Python 3.14 ed esegui `python -m pip install -r requirements.txt`.

2. Crea `.streamlit/secrets.toml`:

   ```toml
   SUPABASE_URL = "https://IL-TUO-PROGETTO.supabase.co"
   SUPABASE_KEY = "LA-TUA-CHIAVE-PUBLISHABLE"
   GROQ_API_KEY = "LA-TUA-CHIAVE-GROQ"
   # GROQ_MODEL = "qwen/qwen3.8-27b"
   ```

   Usa una chiave Supabase **publishable** o la vecchia **anon**; non usare chiavi `service_role` o `sb_secret_...`. `GROQ_MODEL` è facoltativo (predefinito `qwen/qwen3.8-27b`). Senza `GROQ_API_KEY` l'analisi usa l'OCR locale, se disponibile. Non pubblicare mai `secrets.toml` o le chiavi. Prima di usare documenti reali, attiva la Zero Data Retention nell'account Groq dove disponibile. La chiave Free ha quote e l'app non attiva fatturazione.

3. Configura Supabase nel SQL Editor, nell'ordine:

   - `supabase/preflight.sql` per lo stato iniziale;
   - `supabase/migrations/202609250001_requests_security.sql` per struttura e policy;
   - `supabase/verify_security.sql` per verificare RLS, bucket privato e dati preesistenti.

   Se aggiorni un progetto con rimborsi esistenti, conserva prima un backup di database e documenti.

4. Avvia l'app:

   ```bash
   uv run streamlit run app.py
   ```

   Con pip, avvia dallo stesso ambiente virtuale con `python -m streamlit run app.py`.

Per Streamlit Community Cloud collega il repository, seleziona **Python 3.14**, configura gli stessi Secrets ed esegui prima la migrazione.

## Diagnostico Groq isolato

Il diagnostico verifica manualmente l'estrazione con Groq e non è integrato nell'app pubblicata: usa solo documenti sintetici generati in memoria e riutilizza client e payload di produzione (`rimborso_voli/extraction.py`), con le stesse copie JPEG e gli stessi limiti. Serve un account Groq **Free** senza fatturazione. La chiave va inserita solo nel terminale personale, quando richiesta in modo nascosto; non incollarla in chat, variabili d'ambiente, file, Secrets o job CCB. Il comando effettua **una sola richiesta** per caso, senza retry; se ricevi `429`, fermati e rispetta la quota.

Base URL fissa (`https://api.groq.com`; `GROQ_BASE_URL` e `GROQ_API_KEY` sono ignorate), timeout 45 s, retry disattivati, redirect rifiutati, nessun header personalizzato. Se `GROQ_CUSTOM_HEADERS` è presente, anche vuota, il diagnostico si ferma prima di costruire il client. Durante la chiamata il logging delle dipendenze è silenziato e poi ripristinato. I limiti valgono per l'elaborazione locale: al più 16 KiB di un corpo 403 raggiungono la classificazione, al più 1 MiB il parsing della risposta valida.

Esegui i quattro casi uno alla volta:

```bash
uv run python -m tests.check_groq --case ricevuta
uv run python -m tests.check_groq --case imbarco
uv run python -m tests.check_groq --case entrambi
uv run python -m tests.check_groq --case ambiguo
```

L'output riporta esito, durata, confronto campo per campo e codice HTTP con categoria fissa. Il codice di uscita è `0` solo quando tutti i campi coincidono, `1` per una differenza e `2` per un errore o input nascosto non disponibile. I test automatici sono offline e non dimostrano il funzionamento live.

Per HTTP 403 il diagnostico mostra solo categorie fisse: permesso modello bloccato (organizzazione o progetto), permessi API Groq, blocco Cloudflare 1010, oppure configurazione/autorizzazione quando il corpo è assente, troppo grande o senza firma riconosciuta. Sono indicatori diagnostici, non una verifica del provider; il corpo e i messaggi non vengono mostrati e nessun blocco viene aggirato.

## Test

I test Python non richiedono servizi cloud:

```bash
uv run python -m unittest discover -s tests -v
```

Per gli scenari SQL serve Node.js:

```bash
npm --prefix tests/sql ci --ignore-scripts
node --test tests/sql/security.test.mjs
```

## Struttura del progetto

| File o cartella | Contenuto |
| --- | --- |
| `app.py` | Entrypoint sottile per Streamlit e Codespaces |
| `rimborso_voli/` | Interfaccia, validazione, estrazione Groq, OCR, sessioni e repository |
| `supabase/` | Migrazione e verifiche del database |
| `tests/` | Test dell'app, del diagnostico e delle policy SQL |
