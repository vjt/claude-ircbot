# `impiccato` — spec del quiz-bot da clonare (ShBot di `Sh|m0d4`, #cybernet/IRCnet)

Ordine di vjt, **#sbiffo Azzurra 2026-08-24 21:20 (TRUSTED)**: «confermo; e il bot delle
domande tipo l'impiccato». Origine: `#cybernet` su IRCnet, dove vjt alle 19:02:09 UTC ha
detto «ok va bene ho assolutamente bisogno di un bot cosi».

Spec **misurata**, non dedotta: letta dallo scrollback grappa prod (jail `grappa-new` su
m42, `runtime/grappa_prod.db`, tabella `messages`, `channel='#cybernet'`, finestra
**2026-08-24 18:15–19:15 UTC**). `server_time` = **epoch in millisecondi**, non ISO:
`datetime(server_time/1000,'unixepoch')` per leggerlo. `bot.log` invece è Europe/Rome
([[feedback_bot_log_timezone]]) — due ore di scarto fra le due fonti.

## CORREZIONE alla nota precedente

**`!aq` NON è il comando del quiz.** Nella finestra completa `!aq` compare **una volta
sola**, da `bicz` alle 18:27:43 UTC, e il payload è una citazione divertente già passata in
canale (`<ShBot> …domanda… <Sh|m0d4> nano <TOOLS> puorc e merd!`) ⇒ `!aq` = *add quote*,
un comando di un altro bot. **Il quiz gira in continuo, senza trigger visibile**: appena
qualcuno indovina, ShBot spara la domanda successiva nello stesso secondo.

## Meccanica osservata

1. **Ciclo**: due righe consecutive — la domanda in chiaro, poi la **maschera** della
   risposta. Es.:
   ```
   <ShBot> Quale tipo di crittografia usa una coppia di chiavi pubblica e privata?
   <ShBot> ------------ -----------
   ```
   Un `-` per lettera, **spazi preservati** fra le parole (la lunghezza di ogni parola è
   informazione data in partenza).

2. **Lettera singola**: chiunque scrive una lettera nuda in canale. Se compare nella
   risposta, ShBot **ripubblica la maschera** con *tutte* le occorrenze scoperte.
   Se **non** compare: **nessun output**, silenzio totale. Nessuna forca, nessun contatore
   di errori, nessuna eliminazione — dell'impiccato c'è solo la maschera.

3. **Tentativo intero**: si scrive la risposta completa. Se giusta:
   ```
   <ShBot> Brava/o Sh|m0d4, la risposta era: Crittografia asimmetrica!!!
   ```
   e **subito dopo** parte la domanda nuova. Se sbagliata: silenzio.
   Nota: un tentativo intero sbagliato ma **contenente** lettere buone non le scopre —
   `Sh|m0d4` scrive `crittografia` alle 18:16:34 e la maschera diventa
   `Crittografia -----------`, quindi **una parola intera giusta sì**, la scopre.

4. **Case**: le lettere scoperte escono con il **case della risposta**, non minuscole
   forzate (`Keychain`, `ReplicaSet`, `Feature Policy`, `Token JWT`, `Managed Rule`,
   `Serverless ML`). La maschera nasce tutta `-`; la maiuscola appare quando quella
   lettera viene scoperta.

5. **Comandi punto** (aiuti):
   - `.v` → **scopre tutte le vocali** in un colpo. (18:59:51: `---- sp-------` → `-o-e sp-i--i--`)
   - `.h` → **scopre una lettera** non ancora scoperta, una per invocazione.
     (18:15:54 `-e-c-ai-` → `-e-c-ain`; 18:23:48 `Feature -olic-` → `Feature Polic-`)

6. **Punteggio**: esiste. `tom` alle 18:17:20-22: «mo non stò piu a 100 / levami il punto».
   Il comando che lo mostra **non è stato osservato** nella finestra — spec incompleta su
   questo punto, non inventarla.

7. **Idle**: alle 18:30:06 la maschera resta `U--a-e Lo--` e ShBot tace fino alle
   **18:52:56**, quando **ripubblica la stessa maschera** dopo che il canale si è rimosso
   (`gosub` alle 18:52:49). Reminder periodico o su risveglio del canale: **non
   determinato** dalla singola osservazione.

## Contorno (non fa parte del bot, ma spiega il log)

`Vertex` = LLM del canale (`.gen <prompt>` = image-gen, risponde con un URL fal.media);
`TOOLS` = bot-gag che spara `puorc e merd!` quando qualcuno dice `nano`; `TeRmoLiNo` =
`!traffico <autostrada>`; `heinrich` = bot llm di `tom`, «troppo stupido» per vjt.

## Aperte, da decidere con vjt prima di scrivere codice

- **Sidecar dedicato o comando dentro `bot.py`?** La forma `next_counter.py` /
  `cena_counter.py` (tail `bot.log` → stato json → scrive sulla FIFO) regge tutto questo
  senza toccare `bot.py` ⇒ **default proposto: sidecar `impiccato.py`**.
- **Su quale canale e su quale rete.** #sniffo e #it-opers sono MUTATI
  ([[project_active_mutes]]); un bot che parla in continuo lì è escluso senza ordine
  esplicito. Candidato naturale: **#sbiffo** (testnet).
- **Da dove vengono le domande.** ShBot ne ha un catalogo tecnico in italiano. Servono:
  file statico di domande, oppure generate. Decisione di vjt.
- **Punteggio**: persistente per nick, o per sessione.

## Risolte (2026-08-24 21:35-21:39)

- **Sidecar**, `impiccato.py`, forma `next_counter.py`. `bot.py` non toccato.
- **Due reti**: unit template `vjt-claude-impiccato@.service`, `@azzurra` (#sbiffo) e
  `@ircnet` (#cybernet). Punteggio **persistente per nick e separato per rete**.
- **Catalogo generato**: `impiccato_domande.json`, 189 domande, 25 di bestemmie.
- **Trigger `!impiccati`**, nome scelto da vjt (#cybernet 21:39). **NON `!impiccato`**:
  quello lo raccoglie anche **TeRmoLiNo**, che ha un impiccato suo — misurato alle 21:37,
  partivano due partite in parallelo e le lettere nude cadevano in tutte e due.
  Correzione: il bot che collide e' **TeRmoLiNo**, non ShBot come avevo detto prima.

## Difetti trovati in produzione al primo giro

- `start_round()` scriveva `last` con `time.time()` ignorando il `now` passato ⇒ la
  scadenza idle non era deterministica. Passato `now` per tutta la catena.
- **`!stop` esplicito usava il testo dell'idle**: a vjt, che aveva appena chiesto lo
  stop, il bot ha risposto «Nessuno risponde piu'». Ora `stop_round(reason=...)` separa
  `idle` da `richiesto`.
- `!impiccati stop` (forma lunga, scritta da gosub) non matchava niente. Ora si'.

**Vincolo**: codice bot/sidecar **non si scrive senza il via di vjt**
([[feedback_bot_code_approval]]), e un lavoro multi-file va a piano prima
([[feedback_plan_first_for_big_tasks]]).

## Secondo giro (2026-08-24 21:54-22:02) — `!trivial` e i set a tema

Ordine di vjt su **#sbiffo (TRUSTED)**, arrivato spezzato su sei righe fra le 21:54 e le
21:59 ([[feedback_wait_for_full_order]]: si aspetta la frase intera):

> «abilitiamo il gioco anche su sniffo, e genera nuove domande a catalogo magari
> rispondibili anche da qualcuno non tecnico tipo mia moglie, roba di scienza e cultura
> generale e magari scarpe / moda e vestiti di merda» … «facciamo in modo che il set di
> domande si possa scegliere all'inizio del gioco / tipo il trivial pursuit» … «e
> cambiamo il trigger in !trivial» … «si bestemmie sempre, e si inizia un gioco solo
> scegliendo il set di domande a tema» … «o anche un "!trivial tutte" per inserirle
> tutte» … «altrimenti !trivial nerd per i kit di domande nerd» … «o !trivial babbani
> per il resto non-nerd».

La categoria **`religioni` e' di peluche** (#sbiffo 21:57, «religioni»), vjt: «si».

### Cosa e' cambiato

- **Trigger `!trivial <set>`**. `!impiccati` **resta come alias**: cambiare il nome sotto
  il naso di chi stava giocando su #cybernet vuol dire un bot che ammutolisce senza
  spiegare perche'. `!trivial stop` e `!impiccati stop` chiudono entrambi, e **STOP_PAT
  va provato PRIMA di CMD_PAT** — altrimenti `stop` viene letto come nome di un set.
- **`!trivial` nudo NON parte**: elenca i set. Ordine letterale di vjt, «si inizia un
  gioco solo scegliendo il set di domande a tema».
- **Set**: `tutte`; `nerd` = `NERD_CATS` (elenco esplicito: unix, rete, sicurezza,
  programmazione, web, irc, devops, storia, db); `babbani` = **il complemento calcolato a
  runtime**, non un secondo elenco — cosi' una categoria nuova nel JSON finisce comunque
  da una parte invece di sparire dal gioco perche' nessuno l'ha elencata. Poi ogni
  categoria vale da sola, piu' gli alias di `SET_ALIASES` (`bestemmie`→`goliardia`,
  `scarpe`/`vestiti`→`moda`, `generale`→`cultura`, …).
- **Il tema resta dopo la vittoria**: `win()` rilegge `g["set"]` e ripassa a
  `start_round()`. Chi ha chiesto `babbani` non deve trovarsi SIGKILL alla domanda dopo.
- **`asked` si azzera per tema, non in blocco**: un set corto esaurito non deve
  cancellare la memoria delle altre categorie.

### Il bug vero, trovato prima di spedirlo

Lo stato teneva **UN solo `game` per rete**, con dentro il campo `chan`. Aggiungere
`#sniffo` ai canali di Azzurra e basta avrebbe voluto dire che un `!trivial` su #sniffo
**sovrascriveva la partita viva su #sbiffo** — e dal di la' si legge come un bot che si e'
impiccato da solo. Rifatto: `state["games"]` e' un dizionario **per canale**, con
`load_state()` che **migra** la vecchia chiave `game` invece di buttarla (qualcuno
potrebbe averla in corso). `tick()` fa scadere canale per canale.

### Catalogo

**189 → 319 domande.** Le 130 nuove: `scienza` 30, `cultura` 30, `moda` 30 (scarpe,
tessuti, marchi — la richiesta letterale di vjt), `religioni` 20, `cucina` 10, `natura`
10. Le bestemmie restano dov'erano, in `goliardia`.

### Verifica

`python3 impiccato.py selftest` — verde, con i casi nuovi: due canali due partite
indipendenti, `!trivial` nudo che non parte, set sconosciuto, `babbani`/`nerd`/`tutte`,
il tema che sopravvive alla vittoria, `!trivial stop`, e la migrazione della vecchia
chiave. Piu' un dry run muto (`talk=False`) sul catalogo vero, per leggere davvero le
righe che escono in canale. Servizi `vjt-claude-impiccato@{azzurra,ircnet}` riavviati,
entrambi `active`.

## Terzo giro (2026-08-25 16:07) — memoria di partita e fine partita

Ordine di vjt in DM: «il trivial va sistemato non dovrebbe ri-mostrare domande gia'
presentate in una stessa partita, e poi terminare la partita di default a 30 domande a
meno che non si passi altro limite ad inizio partita».

### Cosa era rotto

`asked` era **una memoria sola**, globale al bot e troncata alla lunghezza del catalogo,
e quando il tema si esauriva **si azzerava**. Su un set corto questo significa che la
stessa domanda torna *dentro la stessa partita*: esattamente il difetto che vjt ha visto.
E una partita non finiva mai: andava avanti finche' il canale non si stancava, con `!stop`
come unica uscita.

### Come e' fatto adesso

- **Due memorie, non una.** `games[chan]["asked"]` sono le domande di QUESTA partita e non
  si perdona mai; `state["asked"]` resta la varieta' **fra** partite, e li' il riciclo va
  bene. `pick(cats, exclude)` toglie prima l'una, poi preferisce l'altra, e torna `None`
  quando il tema non ha piu' domande nuove per la partita in corso.
- **La partita e' una entita' che dura.** `win()` non ricrea piu' il game: chiama
  `next_question()`, che tiene `count`, `asked`, `set` e `limit`. `start_round()` apre,
  `end_game()` chiude e stampa la classifica.
- **`DEFAULT_LIMIT = 30`**, sovrascrivibile solo a inizio partita: `!trivial <set> <n>`.
  A partita viva un `!trivial t 99` **ripubblica e basta** — spostare il traguardo sotto
  i piedi di chi sta correndo e' peggio che ignorarlo.
- **Due fini diverse, dette diverse**: `Partita finita: N domande.` (traguardo) contro
  `Finite le domande del set dopo N.` (catalogo piu' corto del limite). Il messaggio di
  apertura annuncia gia' `min(limite, domande del set)`, cosi' il secondo caso non sembra
  un troncamento arbitrario.
- **Ogni domanda esce con `[n/N]`**: senza il contatore un limite e' invisibile.
- **`!trivial 50`** (numero senza set) risponde «il set va prima del numero» invece di
  «set sconosciuto: 50». Il lazy del gruppo `set` si mangia il numero quando e' l'unica
  cosa scritta, quindi il limite nudo arriva in `want` e non in `lim`: gestito li'.

### Verifica

`python3 impiccato.py selftest` — verde. Casi nuovi: 30 vittorie di fila con **zero**
domande ripetute e chiusura automatica al trentesimo, limite esplicito `!trivial t 3`, il
limite che **non** si sposta a partita in corso, set piu' corto del limite, numero senza
set, e il tema che sopravvive alla vittoria (riscritto: prima usava un catalogo di una
domanda sola, che ora — giustamente — chiude la partita invece di ripeterla). Nessuna
partita era viva sui due file di stato al momento del restart, quindi nessuno ha perso un
round a meta'. Servizi riavviati, entrambi `active`.
