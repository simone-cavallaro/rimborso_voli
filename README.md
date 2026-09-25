Caro Voli Sicilia — archivio personale di ricevute e documenti di viaggio
=====================================================================

L'app permette di conservare la ricevuta appena compri un volo e di aggiungere la
carta d'imbarco quando diventa disponibile. Gemini può leggere i documenti e
proporre i dati; puoi sempre compilare manualmente. I dati e i PDF vengono
archiviati su Supabase. La presentazione della domanda sul portale Caro Voli
rimane manuale: l'app non invia richieste al portale e non calcola il rimborso.

Il deploy precedente è raggiungibile su
[Streamlit Community Cloud](https://rimborso-voli-sicilia.streamlit.app/).
Le modifiche locali non aggiornano automaticamente il deploy.

**Come usare il nuovo flusso**

1. Accedi e apri «Nuova richiesta».
2. Carica la ricevuta in JPG/JPEG/PNG. La carta d'imbarco è facoltativa.
3. Se vuoi, premi «Leggi i nuovi documenti con AI». Solo questa azione invia le
   immagini selezionate a Google Gemini. Controlla sempre il costo della singola
   tratta: una prenotazione può contenere più voli o passeggeri.
4. Correggi i campi e salva. Puoi lasciare vuoti quelli che non conosci; i valori
   presenti devono essere validi. La ricevuta è sempre necessaria.
5. In «Storico rimborsi», apri la richiesta e premi «Modifica / aggiungi carta
   d'imbarco». Puoi correggere tutti i dati e aggiungere o sostituire i documenti.
   Senza un nuovo upload, i documenti precedenti vengono conservati.
6. «Prepara download» recupera un singolo documento; «Scarica» lo scarica. Lo
   storico non scarica più tutti i PDF automaticamente.

«Documenti completi» indica solo che ricevuta e carta d'imbarco sono presenti,
non l'approvazione o l'invio del rimborso. Il totale delle spese include tutte le
richieste dell'utente, anche oltre la pagina visualizzata, escludendo gli importi
mancanti o non validi. Lo storico mostra 20 richieste per pagina.

**Aggiornamento obbligatorio di Supabase prima del deploy**

La nuova versione richiede una migrazione: non basta sostituire `app.py`.
La migrazione non è stata eseguita sul progetto cloud da questo workspace.

1. Conserva un backup del database e l'esportazione delle policy esistenti.
2. Applica prima la migrazione in un progetto di staging. Nel SQL Editor esegui
   `supabase/migrations/202609250001_requests_security.sql` con un account
   amministratore del progetto.
3. Esegui `supabase/verify_security.sql` e controlla che RLS sia attiva, il bucket
   `pdf_rimborsi` sia privato e le policy restrittive siano presenti. Il file
   segnala anche dati preesistenti da correggere, senza eliminarli.
4. Prova con due account distinti: ricevuta iniziale, aggiunta carta d'imbarco,
   modifica, download e cancellazione. Verifica che nessun account possa accedere
   ai record o ai percorsi dell'altro, anche tramite API.
5. Applica la migrazione al progetto di produzione, poi distribuisci il codice e
   riavvia l'app. Il riavvio elimina i vecchi client globali dalla memoria.

La migrazione è transazionale e ripetibile. Conserva gli ID esistenti, supporta
sia ID UUID sia serial/bigserial e mantiene i vecchi percorsi dei documenti.
Aggiunge `client_request_id` e `version`, rende facoltativi carta d'imbarco, date
e costo, crea la funzione `richieste_summary()` e protegge identità e documenti.
I nuovi inserimenti e aggiornamenti devono rispettare proprietà dei percorsi e
validità degli importi. Le righe precedenti non conformi rimangono leggibili ma
devono essere corrette quando vengono modificate.

Le policy restrittive limitano anche eventuali policy permissive preesistenti.
Le policy degli altri bucket non vengono rimosse. Altre policy *restrittive*
già presenti possono invece continuare a negare operazioni legittime: vanno
esaminate nell'ambiente reale. L'app precedente potrebbe non riuscire più a
sovrascrivere file durante l'intervallo fra migrazione e nuovo deploy; pianifica
l'aggiornamento di conseguenza.

**Configurazione locale e deploy**

Usa Python 3.14 e `uv`. `pyproject.toml` e `uv.lock` sono la fonte delle dipendenze;
`requirements.txt` è esportato dal lockfile per gli ambienti che usano pip.

```powershell
uv sync --locked
uv run streamlit run app.py
```

Crea `.streamlit/secrets.toml` localmente oppure configura gli stessi valori nei
Secrets di Streamlit Community Cloud:

```toml
SUPABASE_URL = "https://IL-PROGETTO.supabase.co"
SUPABASE_KEY = "LA-CHIAVE-PUBLISHABLE-O-ANON"
GEMINI_API_KEY = "LA-CHIAVE-GEMINI"
GEMINI_MODEL = "models/gemini-3.6-flash" # facoltativo
```

La chiave Supabase deve essere una **publishable** (`sb_publishable_...`) oppure
la vecchia chiave JWT **anon**. Le chiavi `service_role` e `sb_secret_...` vengono
rifiutate perché non sono adatte a questo modello di autorizzazione.
La chiave Gemini è necessaria solo per l'analisi, non per salvare manualmente.
Non inserire credenziali nel codice o nei commit. `.gitignore` esclude i secrets,
ma include `.streamlit/config.toml`, che mantiene CORS/XSRF attivi e limita gli
upload a 10 MB per file. Il limite applicativo è inoltre 12 megapixel per immagine.

Per rigenerare i requirements dopo un aggiornamento deliberato del lockfile:

```powershell
uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements-txt --output-file requirements.txt
```

**Protezione dei dati implementata**

- Un client Supabase e uno storage di autenticazione distinti per sessione;
  identità verificata sul server e stato locale ripulito al logout.
- Query di lettura, modifica, cancellazione e recupero documenti filtrate per
  proprietario, oltre alle policy RLS del database e dello storage.
- Percorsi con UUID, upload senza sovrascrittura e controllo della versione prima
  di applicare modifiche simultanee. I metadati del volo non determinano i nomi.
- Ripetizione sicura della creazione tramite `client_request_id`; recupero di
  alcuni casi in cui il server salva ma la risposta di rete viene persa.
- Sostituzione del collegamento al documento solo dopo un upload riuscito;
  rimozione dei vecchi file solo dopo il salvataggio e se non sono più referenziati.
  Sono protetti anche i file condivisi tra duplicati creati dalla vecchia versione.
- Date, prezzi e risposta AI validati; errori non convertiti silenziosamente in
  zero. Il risultato AI viene invalidato quando cambiano i file caricati.
- Verifica del formato reale delle immagini, limite di dimensioni, rotazione EXIF
  e ricodifica dei pixel prima della conversione PDF: metadati e contenuti
  aggiunti in coda al file originale non vengono conservati.
- SDK Google `google-genai`, schema JSON, limite di output, timeout, chiamate a
  strumenti disabilitate e istruzioni per ignorare comandi contenuti nei documenti.
- Dettagli delle eccezioni dei provider non mostrati agli utenti né inseriti nei
  log applicativi; nei log compaiono operazione e classe dell'errore.

La sicurezza del deploy dipende anche da impostazioni esterne non modificabili
dal codice locale: conferma email e protezioni anti-abuso di Supabase Auth,
password policy, quote/budget Gemini, HTTPS e accessi amministrativi ai progetti.
Configura queste impostazioni prima di aprire il servizio ad altri utenti.
La pausa di 15 secondi tra analisi è locale alla sessione e **non** sostituisce un
limite centralizzato per account o un limite di spesa del provider.

Database e Storage non condividono una transazione. In caso di guasto persistente
o arresto del processo possono rimanere file orfani; l'app prova a ripulirli e
privilegia la conservazione dei documenti quando l'esito del salvataggio è incerto.
La bonifica operativa deve confrontare i file con tutti i riferimenti correnti
prima di cancellare. Non viene eseguita una cancellazione massiva automatica.

**Test riproducibili senza servizi cloud**

```powershell
uv run python -m unittest discover -s tests -v
```

I test Python verificano validazione, immagini, sessioni, gestione degli errori,
salvataggi concorrenti, paginazione e il percorso utente con Streamlit AppTest.
Le API sono simulate. Un test usa il vero SDK Google con trasporto HTTP locale,
senza richieste a Gemini.

I test SQL usano PostgreSQL compilato in WebAssembly tramite PGlite. Sono solo
strumenti di sviluppo: Node non serve per eseguire l'app.

```powershell
npm --prefix tests/sql ci --ignore-scripts
node --test tests/sql/security.test.mjs
```

Gli scenari SQL applicano due volte la migrazione a uno schema nuovo e a uno
schema preesistente con ID numerici. Verificano RLS, accesso anonimo, isolamento
tra due utenti, policy permissive preesistenti, aggiornamenti concorrenti,
aggregati privati e protezione dei documenti referenziati. Gli schemi di supporto
Supabase sono simulati: questi test non sostituiscono la prova nel progetto cloud.

`tests/check_models.py` è un diagnostico separato: se eseguito esplicitamente,
contatta Gemini con la chiave locale per elencare i modelli disponibili.

**Organizzazione del codice**

| File | Responsabilità |
|---|---|
| `app.py` | Login, caricamento, revisione, storico e modifica. |
| `domain.py` | Validazione dei dati e preparazione delle immagini/PDF. |
| `security.py` | Client per sessione, controlli di configurazione e log. |
| `extraction.py` | Chiamata Gemini e schema della risposta. |
| `repository.py` | Persistenza, proprietà dei record e gestione dei file. |
| `supabase/` | Migrazione e verifiche del backend. |
| `tests/` | Test Python, test SQL e diagnostico dei modelli. |

Riferimenti tecnici: [sessioni e cache Streamlit](https://docs.streamlit.io/develop/api-reference/caching-and-state/st.cache_resource),
[RLS Supabase](https://supabase.com/docs/guides/database/postgres/row-level-security),
[accesso allo Storage](https://supabase.com/docs/guides/storage/security/access-control),
[SDK Google GenAI](https://ai.google.dev/gemini-api/docs/migrate).
