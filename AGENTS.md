# Istruzioni del team


Prima di scrivere o modificare codice, carica e applica `karpathy-guidelines`. Esplicita le assunzioni rilevanti, scegli la modifica minima completa, limita il diff al compito e verifica il risultato con controlli proporzionati. Mantieni Ponytail e Ponytail-review. Includi questa direttiva nel brief dei subagenti che scrivono codice. Per ricerca e revisione in sola lettura la skill resta disponibile su richiesta.

## Ruolo QA

Freebuff QA sostituisce OpenRouter QA negli incarichi futuri. Riceve esclusivamente scenari sintetici o materiale pubblico, senza codice inedito, credenziali, documenti reali o strumenti; non sostituisce Security. Il vecchio ruolo OpenRouter QA è ritirato. Avviare Freebuff soltanto dopo integrazione CCB verificata, isolamento e test di comunicazione; non mascherarlo come provider diverso e non avviare client esterni al coordinamento CCB.

## Messaggi dei commit

I messaggi dei commit destinati a GitHub devono essere in italiano, sia nel titolo sia nel corpo, inclusi eventuali commit di merge o squash. Nomi tecnici, identificatori e percorsi conservano la forma originale. Questa regola non autorizza commit, push, merge o deploy: restano necessarie le autorizzazioni distinte previste dal progetto.

## Rilasci

`master` è il ramo ufficiale seguito dall'app online. I rilasci autorizzati devono confluire in `master` tramite pull request; pubblicare soltanto un ramo di lavoro non completa il rilascio.

## Stabilità e consegne
Il supervisor è l'unico coordinatore. Avvia i worker con `./team-agent NOME --wait 60`:
attendi lo stato ready prima di inviare il lavoro con ask. Se pending, conserva la richiesta
e ricontrolla; non avviare direttamente altri client, team o subagenti nativi in parallelo.
Al massimo tre client, supervisor incluso.
Non avviare mai ./start-team.sh interattivo da uno strumento dell'agente.
Per avvii automatici esterni: ./start-team.sh --headless --shutdown when-idle.
Non aprire il laboratorio mentre è attivo rimborso_voli, né viceversa.
Usa ask ordinario per incarichi con esito atteso; --chain solo quando un incarico
attivo del supervisor dipende dal figlio. Non usare --silence per incarichi di lavoro.
Il figlio chiude il turno con ID, stato, risultato, test svolti e blocchi residui.
La risposta finale torna tramite il job originale: non inviare una seconda notifica.
Deduplica per ID; in caso di timeout verifica ccb trace e recupera il risultato
registrato prima di valutare una nuova esecuzione. Mai ripetere automaticamente un job.
Il supervisor continua le azioni autorizzate dopo la consegna o comunica il blocco.
Le richieste di accesso/consenso restano visibili; non aggirarle. Il team si arresta
quando liberi dopo la chiusura dell'ultimo terminale. Controlla ./team-status per
stato, risorse e punto di ripresa; ./stop-team.sh arresta esplicitamente il team.
