# ✈️ Caro Voli Sicilia: organizza i documenti per il tuo rimborso voli

Un'applicazione web sviluppata con **Streamlit** per conservare ricevute e carte d'imbarco, estrarre i dati dei voli con **Groq** e preparare i documenti necessari alla richiesta di rimborso sul portale Caro Voli Sicilia.

La domanda sul portale resta a carico dell'utente: l'app **non invia richieste di rimborso** e non ne calcola l'importo.

## 🌐 Usa l'app online

L'app è disponibile su [**rimborso-voli-sicilia.streamlit.app**](https://rimborso-voli-sicilia.streamlit.app/). Non occorre installare nulla: basta creare un account e accedere.

Ogni utente può consultare solo i propri rimborsi e documenti. I dati sono archiviati su Supabase, con controlli di accesso applicati sia al database sia ai file.

L'interfaccia è ottimizzata anche per lo smartphone: caricamenti uno sotto l'altro, anteprime a larghezza piena, pulsanti estesi e navigazione «Nuova richiesta» / «Storico rimborsi» direttamente nel corpo della pagina, insieme al pulsante «Esci». Nessun menu nascosto da aprire su telefono.

## Il problema

La ricevuta di acquisto è disponibile subito, mentre la carta d'imbarco arriva spesso solo poco prima del volo. Nel frattempo possono passare mesi: ritrovare la ricevuta, ricopiare i dati e preparare i PDF diventa scomodo.

## Come funziona

1. **Salva la ricevuta:** carica un'immagine JPG, JPEG o PNG della ricevuta di pagamento. La carta d'imbarco non è necessaria in questa fase.
2. **Compila i dati:** inseriscili manualmente oppure avvia l'analisi automatica. L'app usa sempre Groq come motore principale. L'OCR locale viene usato solo nei casi previsti: Groq non configurato, errore temporaneo del servizio, quota o limite del servizio raggiunto, errore di rete o risposta non valida. Per un errore di configurazione, autorizzazione o modello l'analisi si interrompe, mostra un messaggio fisso e resta la compilazione manuale: nessun errore permanente viene trasformato in un risultato OCR. Le immagini vengono inviate a Groq solo quando avvii l'analisi, come copie ridotte (massimo 2048 pixel per lato, JPEG qualità 90).
3. **Controlla e correggi:** verifica sempre i dati proposti, in particolare il costo della singola tratta quando una prenotazione comprende più voli o passeggeri.
4. **Completa in seguito:** quando ricevi la carta d'imbarco, apri il rimborso nello storico e aggiungila. Puoi anche modificare i dati o sostituire i documenti già caricati.
5. **Scarica i PDF:** l'app prepara i documenti in formato PDF, pronti per essere utilizzati durante la compilazione sul portale.

Nello storico, **«Documenti completi»** significa soltanto che sono presenti sia la ricevuta sia la carta d'imbarco: non indica che la domanda sia stata inviata o approvata.

## Tecnologie utilizzate

- **Interfaccia e logica applicativa:** [Streamlit](https://streamlit.io/) e Python
- **Estrazione dei dati:** API Groq tramite SDK ufficiale (`groq`), fallback OCR con Tesseract e compilazione manuale
- **Database e autenticazione:** PostgreSQL e [Supabase](https://supabase.com/)
- **Archivio documenti:** Supabase Storage
- **Elaborazione delle immagini e PDF:** Pillow e `img2pdf`
- **Hosting:** Streamlit Community Cloud

## Privacy e sicurezza

L'accesso richiede un account. Le policy **Row Level Security (RLS)** limitano le operazioni sui rimborsi al rispettivo proprietario; il bucket dei documenti è privato e consente l'accesso ai file dell'utente autenticato. L'app usa un client Supabase distinto per sessione, valida dati e immagini caricati e non richiede chiavi Supabase privilegiate.

L'analisi automatica è facoltativa: le immagini vengono inviate a Groq solo quando premi il pulsante di analisi, e la connessione viene verificata soltanto in quel momento. Il fallback OCR gira sul server che ospita l'app, senza inviare i documenti a un'altra API. Controlla sempre il risultato prima di salvarlo: l'estrazione automatica può commettere errori o lasciare vuoti i campi incerti.

## 💻 Esegui l'app in locale

Servono **Python 3.14**, [`uv`](https://docs.astral.sh/uv/) e un progetto Supabase configurato. La chiave API Groq è facoltativa. Per usare anche l'OCR in locale, installa Tesseract con le lingue italiana e inglese e rendi disponibile il comando `tesseract` nel PATH; su Streamlit Cloud viene installato automaticamente tramite `packages.txt`.

1. Clona il repository e installa le dipendenze:

   ```bash
   git clone https://github.com/simone-cavallaro/rimborso_voli.git
   cd rimborso_voli
   uv sync --locked
   ```

2. Crea il file `.streamlit/secrets.toml`:

   ```toml
   SUPABASE_URL = "https://IL-TUO-PROGETTO.supabase.co"
   SUPABASE_KEY = "LA-TUA-CHIAVE-PUBLISHABLE"
   GROQ_API_KEY = "LA-TUA-CHIAVE-GROQ"
   # GROQ_MODEL = "qwen/qwen3.8-27b"
   ```

   Usa una chiave Supabase **publishable** oppure la vecchia chiave **anon**. Non usare chiavi `service_role` o `sb_secret_...`. `GROQ_MODEL` è facoltativo: il valore predefinito è `qwen/qwen3.8-27b`. Senza `GROQ_API_KEY`, l'analisi automatica usa direttamente l'OCR (se Tesseract è installato) e l'app lo segnala chiaramente. Non pubblicare mai `secrets.toml` o le chiavi nel repository.

   Prima di inviare documenti reali, attiva nel tuo account Groq la conservazione dei dati zero (Zero Data Retention) dove disponibile; questa operazione va fatta direttamente nell'account, non dall'app. La chiave Free è soggetta a quote: l'app non attiva alcuna fatturazione automatica e, al raggiungimento del limite, passa all'OCR locale come descritto sopra.

   Se l'analisi continua a usare l'OCR, controlla il messaggio mostrato dall'app: indica se la configurazione manca, se il servizio è temporaneamente non disponibile, se è stata raggiunta una quota o un limite, o se si è verificato un errore di rete o una risposta non valida. Se l'analisi si interrompe con un errore di configurazione, autorizzazione o modello, verifica le impostazioni indicate in questa sezione. In pagina è riportato solo se l'analisi è configurata: la connessione reale viene verificata soltanto all'avvio dell'analisi.

3. Configura Supabase prima di avviare una nuova installazione. Nel SQL Editor esegui, nell'ordine:

   - `supabase/preflight.sql` per controllare lo stato iniziale;
   - `supabase/migrations/202609250001_requests_security.sql` per creare o aggiornare struttura e policy;
   - `supabase/verify_security.sql` per verificare RLS, bucket privato e dati preesistenti.

   Se aggiorni un progetto che contiene già rimborsi, conserva prima un backup del database e dei documenti. La migrazione non elimina automaticamente le richieste esistenti.

4. Avvia l'app:

   ```bash
   uv run streamlit run app.py
   ```

Per pubblicarla su Streamlit Community Cloud, collega il repository, seleziona **Python 3.14**, configura gli stessi Secrets nella dashboard ed esegui prima la migrazione sul progetto Supabase collegato all'app.

## Diagnostico Groq isolato

Questa prova serve solo a verificare manualmente l'estrazione con Groq. Non è integrata nell'app pubblicata e non invia dati reali: usa esclusivamente documenti sintetici generati in memoria. Riutilizza il client e il payload di produzione (`extraction.py`), quindi invia esattamente le stesse richieste dell'app, con le stesse copie JPEG e gli stessi limiti.

Serve un account Groq **Free** senza fatturazione attiva. La chiave API va inserita solo nel terminale personale, quando richiesta in modo nascosto: non incollarla nella chat, in variabili d'ambiente, in file, nei Secrets o in un job CCB. Il diagnostico effettua **una sola richiesta per comando**, senza retry; se riceve `429`, fermati e rispetta la quota. La modalità JSON con immagini è documentata in <https://console.groq.com/docs/vision>.

La richiesta usa l'SDK ufficiale Groq (`groq==1.7.0`, dipendenza di produzione di questo progetto): base URL fisso (`https://api.groq.com`; le variabili `GROQ_BASE_URL` e `GROQ_API_KEY` sono ignorate, la chiave arriva solo dall'input nascosto), timeout 45 s, retry disattivati, redirect rifiutati, nessun header o firma personalizzata. Durante la creazione, la chiamata e la chiusura del client il logging è temporaneamente silenziato e ripristinato alla fine (l'SDK e il suo stack HTTP scrivono record di debug con opzioni di richiesta e dettagli di eccezione, e `GROQ_LOG` può alzarne la verbosità); il logging dell'app e del server non viene modificato. Nell'app, invece del silenciamento globale, i logger delle dipendenze (`groq`, `groq._base_client`, `groq._response`, `httpx`, `httpcore.*`) sono filtrati alla sorgente con un contesto per richiesta: durante una chiamata Groq i loro record — che possono contenere opzioni, URL, intestazioni e dettagli di eccezione — non raggiungono alcun handler, mentre le altre sessioni e i log applicativi continuano normalmente.

Se la variabile `GROQ_CUSTOM_HEADERS` è presente nell'ambiente, anche vuota, il diagnostico si interrompe prima di costruire il client: potrebbe sovrascrivere autenticazione e identità del client. Per eseguire la prova, usa un terminale in cui questa variabile non sia impostata. Il suo contenuto non viene mostrato.

Limiti sui dati ricevuti: l'SDK legge i corpi di risposta e di errore in memoria prima che il diagnostico li esamini, quindi nessun limite al download è garantito dal diagnostico. I limiti si applicano a ciò che viene poi elaborato in locale: al più 16 KiB del corpo di un errore 403 raggiungono la classificazione, al più 1 MiB il parsing della risposta andata a buon fine; corpi più grandi producono la categoria generica.

Esegui i quattro casi uno alla volta:

```bash
uv run python -m tests.check_groq --case ricevuta
uv run python -m tests.check_groq --case imbarco
uv run python -m tests.check_groq --case entrambi
uv run python -m tests.check_groq --case ambiguo
```

L'output riporta solo esito, durata, confronto campo per campo e codice HTTP con categoria fissa. Il codice di uscita è `0` solo quando tutti i campi coincidono; un valore diverso indica una differenza o un errore. Se il terminale non consente l'input nascosto, il comando si ferma prima di leggere la chiave. I test automatici sono offline e non dimostrano il funzionamento live: la prova con Groq resta manuale.

### Cosa significa HTTP 403 nel diagnostico

Solo per HTTP 403, il diagnostico classifica una porzione del corpo già ricevuto dall'SDK, limitata a 16 KiB più un byte di controllo; se supera 16 KiB la scarta. Mostra esclusivamente categorie fisse:

- **Permesso modello bloccato (organizzazione/progetto):** `permissions_error` con codice `model_permission_blocked_org` o `model_permission_blocked_project`. Verifica l'accesso al modello con l'amministratore nella console Groq.
- **Permessi API Groq:** `permissions_error` senza un codice riconosciuto. Verifica i permessi con l'amministratore.
- **Blocco accesso Cloudflare 1010:** testo completo `error code: 1010` (spazi esterni ammessi) oppure JSON con `error_code` 1010, `error_name` `browser_signature_banned` e `cloudflare_error` true. Contatta l'assistenza Groq.
- **Configurazione/autorizzazione:** corpo assente, troppo grande, illeggibile o senza una firma riconosciuta; la causa resta da accertare.

Questi sono indicatori diagnostici, non una verifica dell'autenticità del provider. Il corpo e i messaggi ricevuti non vengono mostrati. Il diagnostico non modifica chiave, IP o firma client e non aggira blocchi.

## Test

I test Python non richiedono l'accesso ai servizi cloud:

```bash
uv run python -m unittest discover -s tests -v
```

Per verificare anche gli scenari SQL serve Node.js:

```bash
npm --prefix tests/sql ci --ignore-scripts
node --test tests/sql/security.test.mjs
```

## Struttura del progetto

| File o cartella | Contenuto |
| --- | --- |
| `app.py` | Interfaccia, caricamento, revisione e storico |
| `domain.py` | Validazione dei dati e preparazione delle immagini |
| `extraction.py` | Estrazione dei dati con Groq |
| `ocr_fallback.py` e `packages.txt` | OCR locale e dipendenze di sistema |
| `security.py` | Configurazione e sessioni utente |
| `repository.py` | Salvataggio di rimborsi e documenti |
| `supabase/` | Migrazione e verifiche del database |
| `tests/` | Test dell'app e delle policy SQL |
