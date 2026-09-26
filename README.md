# ✈️ Caro Voli Sicilia: organizza i documenti per il tuo rimborso voli

Un'applicazione web sviluppata con **Streamlit** per conservare ricevute e carte d'imbarco, estrarre i dati dei voli con **Gemini** e preparare i documenti necessari alla richiesta di rimborso sul portale Caro Voli Sicilia.

La domanda sul portale resta a carico dell'utente: l'app **non invia richieste di rimborso** e non ne calcola l'importo.

## 🌐 Usa l'app online

L'app è disponibile su [**rimborso-voli-sicilia.streamlit.app**](https://rimborso-voli-sicilia.streamlit.app/). Non occorre installare nulla: basta creare un account e accedere.

Ogni utente può consultare solo i propri rimborsi e documenti. I dati sono archiviati su Supabase, con controlli di accesso applicati sia al database sia ai file.

## Il problema

La ricevuta di acquisto è disponibile subito, mentre la carta d'imbarco arriva spesso solo poco prima del volo. Nel frattempo possono passare mesi: ritrovare la ricevuta, ricopiare i dati e preparare i PDF diventa scomodo.

## Come funziona

1. **Salva la ricevuta:** carica un'immagine JPG, JPEG o PNG della ricevuta di pagamento. La carta d'imbarco non è necessaria in questa fase.
2. **Compila i dati:** inseriscili manualmente oppure usa Gemini per proporre le informazioni ricavate dai documenti. Le immagini vengono inviate a Gemini solo quando avvii l'analisi.
3. **Controlla e correggi:** verifica sempre i dati proposti, in particolare il costo della singola tratta quando una prenotazione comprende più voli o passeggeri.
4. **Completa in seguito:** quando ricevi la carta d'imbarco, apri il rimborso nello storico e aggiungila. Puoi anche modificare i dati o sostituire i documenti già caricati.
5. **Scarica i PDF:** l'app prepara i documenti in formato PDF, pronti per essere utilizzati durante la compilazione sul portale.

Nello storico, **«Documenti completi»** significa soltanto che sono presenti sia la ricevuta sia la carta d'imbarco: non indica che la domanda sia stata inviata o approvata.

## Tecnologie utilizzate

- **Interfaccia e logica applicativa:** [Streamlit](https://streamlit.io/) e Python
- **Estrazione dei dati:** API Gemini, con possibilità di compilazione manuale
- **Database e autenticazione:** PostgreSQL e [Supabase](https://supabase.com/)
- **Archivio documenti:** Supabase Storage
- **Elaborazione delle immagini e PDF:** Pillow e `img2pdf`
- **Hosting:** Streamlit Community Cloud

## Privacy e sicurezza

L'accesso richiede un account. Le policy **Row Level Security (RLS)** limitano le operazioni sui rimborsi al rispettivo proprietario; il bucket dei documenti è privato e consente l'accesso ai file dell'utente autenticato. L'app usa un client Supabase distinto per sessione, valida dati e immagini caricati e non richiede chiavi Supabase privilegiate.

L'analisi con Gemini è facoltativa. Se la usi, controlla il risultato prima di salvarlo: l'estrazione automatica può commettere errori o non trovare alcuni campi.

## 💻 Esegui l'app in locale

Servono **Python 3.14**, [`uv`](https://docs.astral.sh/uv/), un progetto Supabase configurato e, solo se vuoi usare l'analisi automatica, una chiave API Gemini.

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
   GEMINI_API_KEY = "LA-TUA-CHIAVE-GEMINI"
   # GEMINI_MODEL = "models/gemini-3.6-flash"
   ```

   Usa una chiave Supabase **publishable** oppure la vecchia chiave **anon**. Non usare chiavi `service_role` o `sb_secret_...`. `GEMINI_API_KEY` è facoltativa se compili i dati manualmente. Non pubblicare mai `secrets.toml` o le chiavi nel repository.

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
| `extraction.py` | Estrazione dei dati con Gemini |
| `security.py` | Configurazione e sessioni utente |
| `repository.py` | Salvataggio di rimborsi e documenti |
| `supabase/` | Migrazione e verifiche del database |
| `tests/` | Test dell'app e delle policy SQL |