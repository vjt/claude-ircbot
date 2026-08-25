#!/usr/bin/env python3
"""impiccato — quiz a maschera sul modello di ShBot (#cybernet, IRCnet).

Ordine di vjt: #sbiffo 2026-08-24 21:20 «confermo; e il bot delle domande tipo
l'impiccato», dettagli alle 21:27 («sidecar si; le domande GENERALE, un gran
bel catalogo incluse le bestemmie; si persistente per nick») e alle 21:29
(«anche su cybernet ovviamente»).

Stessa forma di next_counter.py / cena_counter.py: tail del bot.log, match dei
PRIVMSG in canale, stato su json, risposte scritte sulla FIFO del bot. `bot.py`
NON viene toccato.

DUE RETI, DUE PROCESSI. Azzurra e IRCnet sono istanze separate di bot.py con
log e FIFO propri, quindi il sidecar e' parametrico sulla rete e se ne lancia
uno per ciascuna. I punteggi sono per-rete di proposito: su IRCnet non esistono
i services, un nick li' non prova niente (vedi feedback_spoofed_identity_refusal)
e mescolare le classifiche regalerebbe a chiunque i punti di un omonimo.

Meccanica, MISURATA sullo scrollback di ShBot e non dedotta — la spec completa
con le righe di log sta in docs/impiccato_spec.md:

  * Ogni giro sono due righe: la domanda, poi la maschera della risposta. Un
    `-` per ogni lettera, gli spazi fra le parole restano visibili (la
    lunghezza di ogni parola e' informazione data in partenza).
  * Lettera nuda in canale: se c'e', si scoprono TUTTE le sue occorrenze e la
    maschera viene ripubblicata. Se non c'e', SILENZIO — niente forca, niente
    contatore di errori, niente eliminazione. Dell'impiccato c'e' solo la
    maschera, ed e' voluto: cosi' il gioco non ha perdenti e il canale non si
    riempie di "hai sbagliato".
  * Parola intera giusta: scopre quella parola (ShBot lo fa — `crittografia`
    alle 18:16:34 UTC ha portato la maschera a `Crittografia -----------`).
  * Risposta intera giusta: `Brava/o <nick>, la risposta era: <X>!!!` e la
    domanda dopo parte subito.
  * `.v` scopre tutte le vocali, `.h` scopre UNA lettera a caso fra quelle
    ancora coperte.
  * Il case della risposta e' preservato: `ReplicaSet`, `Token JWT`.

Quello che ShBot fa e qui NON e' replicato, perche' non l'ho misurato: il
comando che mostra il punteggio. `!classifica` e' roba mia, non sua.

Aggiunte mie, dichiarate: il gioco si ferma da solo dopo IDLE_STOP secondi di
silenzio invece di sparare domande per sempre a canale vuoto, e si riavvia con
`!trivial <set>`. Un bot che parla in eterno in un canale e' un flood, non un
gioco.

I SET A TEMA sono ordine di vjt (#sbiffo 21:57-21:59, «tipo il trivial
pursuit»): si gioca con `!trivial <set>` e il trigger nudo elenca soltanto,
perche' «si inizia un gioco solo scegliendo il set di domande a tema». Vedi
SET_ALIASES e resolve_set(). `!impiccati` resta accettato come alias.

CLI:
  impiccato.py --net azzurra            avvia il daemon su Azzurra (#sbiffo)
  impiccato.py --net ircnet             avvia il daemon su IRCnet (#cybernet)
  impiccato.py --net azzurra classifica stampa la classifica e esce
  impiccato.py --net azzurra selftest   esercita la meccanica senza rete ne' FIFO

Env override (un dry run non deve poter parlare):
  IMPICCATO_LOG    path del bot.log da seguire
  IMPICCATO_FIFO   path della FIFO del bot ('' zittisce del tutto)
  IMPICCATO_STATE  path dello stato json
  IMPICCATO_CHANS  canali, separati da virgola
"""
import json
import os
import random
import re
import select
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent
DOMANDE = REPO / "impiccato_domande.json"

# Una voce per rete. Sono processi di bot.py distinti: log, FIFO e canali
# separati, quindi anche stato separato.
NETS = {
    "azzurra": {
        "log": REPO / "bot.log",
        "fifo": REPO / "bot.send",
        "state": REPO / "impiccato_state.json",
        # #sniffo aggiunto per ordine di vjt (#sbiffo 2026-08-24 21:54).
        # Le partite sono per canale: vedi load_state().
        "chans": {"#sbiffo", "#sniffo"},
    },
    "ircnet": {
        "log": REPO / "bot.ircnet.log",
        "fifo": REPO / "bot.send.ircnet",
        "state": REPO / "impiccato_state.ircnet.json",
        "chans": {"#cybernet"},
    },
}

# Silenzio in canale dopo il quale la partita si chiude da sola.
IDLE_STOP = 600
# Ogni quanto il loop si sveglia comunque, per poter far scadere l'idle anche
# quando sul log non passa piu' niente.
TICK = 20
# Le lettere che `.v` scopre in un colpo solo.
VOCALI = set("aeiou")
# Quante domande dura una partita se nessuno chiede altro. Ordine di vjt (DM
# 2026-08-25 16:07): «terminare la partita di default a 30 domande a meno che
# non si passi altro limite ad inizio partita». Senza un fondo la partita
# durava finche' il canale non si stancava, e il `!stop` era l'unica uscita.
# Portato da 30 a 10 su suo ordine (#sbiffo 2026-08-25 19:56): 30 domande sono
# una maratona, il canale si stanca prima del traguardo.
DEFAULT_LIMIT = 10

PRIVMSG_PAT = re.compile(
    r'< :(?P<nick>[^!@\s]+)!(?P<ident>[^@\s]+)@(?P<host>\S+)\s+'
    r'PRIVMSG\s+(?P<chan>#\S+)\s+:(?P<text>.*?)$'
)

# Il trigger NON e' `!impiccato`: su #cybernet lo raccoglie anche TeRmoLiNo,
# che ha un impiccato suo, e partivano due partite in parallelo con le lettere
# nude che cadevano in tutte e due (misurato in canale alle 21:37, vjt: «cambio
# trigger», e il nome e' suo: «usiamo !impiccati», 21:39). Non collide con
# nessuno degli altri bot del canale.
#
# 21:58, sempre vjt: «e cambiamo il trigger in !trivial». `!impiccati` resta
# come alias e basta: cambiare il nome sotto il naso di chi sta giocando su
# #cybernet significa un bot che smette di rispondere senza dire perche'.
# Il numero finale e' il limite di domande della partita: `!trivial nerd 50`.
# Il gruppo `set` e' lazy, quindi le cifre in coda finiscono in `limit` e non
# dentro il nome del set.
#
# `!trivia` e' alias di `!trivial` (ordine di vjt, #sniffo 2026-08-25 23:23,
# dopo l'obiezione di peluche: in inglese il quiz e' `trivia`, `trivial` vuol
# dire banale). La `l` finale resta opzionale nel pattern invece di aggiungere
# una terza alternativa: una `?` non puo' divergere dal ramo accanto.
CMD_PAT = re.compile(
    r'^!\s*(?:trivial?|impiccati)(?:\s+(?P<set>.+?))?(?:\s+(?P<limit>\d{1,3}))?\s*$',
    re.IGNORECASE)
CLASSIFICA_PAT = re.compile(r'^!\s*classifica\s*$', re.IGNORECASE)
# `!impiccati stop` va accettato quanto `!stop`: gosub ha scritto la forma
# lunga al primo giro (#cybernet 21:38) e cadeva nel vuoto, che si legge come
# un bot che ignora chi vuole chiudere.
STOP_PAT = re.compile(r'^!\s*(?:stop|basta|(?:trivial?|impiccati)\s+stop)\s*$',
                      re.IGNORECASE)

# I set a tema. Ordine di vjt (#sbiffo 2026-08-24 21:57-21:59): «facciamo in
# modo che il set di domande si possa scegliere all'inizio del gioco, tipo il
# trivial pursuit», «!trivial tutte per inserirle tutte», «!trivial nerd per i
# kit di domande nerd», «!trivial babbani per il resto non-nerd».
#
# `nerd` e' l'elenco esplicito; `babbani` e' il suo complemento calcolato a
# runtime, cosi' una categoria nuova nel JSON finisce automaticamente da una
# parte e non sparisce dal gioco perche' nessuno si e' ricordato di elencarla.
NERD_CATS = {"unix", "rete", "sicurezza", "programmazione", "web", "irc",
             "devops", "storia", "db"}
# Nomi alternativi accettati per un set. Chi scrive `!trivial bestemmie` non
# deve trovare il vuoto.
SET_ALIASES = {
    "tutto": "tutte", "all": "tutte", "misto": "tutte",
    "bestemmie": "goliardia", "goliardiche": "goliardia",
    "tech": "nerd", "tecniche": "nerd", "informatica": "nerd",
    "babbano": "babbani", "non-nerd": "babbani",
    "generale": "cultura", "scarpe": "moda", "vestiti": "moda",
    "religione": "religioni", "animali": "natura", "cibo": "cucina",
}
HINT_PAT = re.compile(r'^\.\s*h\s*$', re.IGNORECASE)
VOWEL_PAT = re.compile(r'^\.\s*v\s*$', re.IGNORECASE)

NICK_ALIASES = {
    "vjt`afk": "vjt", "vjt`zzz": "vjt", "vjt42": "vjt", "vjt_": "vjt",
}


def canon_nick(n):
    return NICK_ALIASES.get(n.casefold(), n)


def fold(s):
    """Casefold + via gli accenti: 'È' e 'e' sono la stessa lettera per il
    gioco, altrimenti indovinare 'perche'' diventa un terno al lotto."""
    d = unicodedata.normalize("NFD", s)
    return "".join(c for c in d if not unicodedata.combining(c)).casefold()


def norm_answer(s):
    """Forma di confronto per un tentativo intero: niente accenti, niente
    punteggiatura, spazi collassati."""
    s = fold(s)
    s = re.sub(r'[^0-9a-z]+', ' ', s)
    return " ".join(s.split())


def is_hidden(ch):
    """Solo gli alfanumerici si nascondono. Spazi, apostrofi e trattini
    restano visibili: sono la struttura della risposta, non il contenuto."""
    return ch.isalnum()


# ---------------------------------------------------------------- catalogo

def load_domande():
    doc = json.loads(DOMANDE.read_text(encoding="utf-8"))
    out = []
    for d in doc["domande"]:
        q, a = d["q"].strip(), d["a"].strip()
        if q and a and any(is_hidden(c) for c in a):
            # `alt`: altre risposte da accettare. Una domanda tecnica ha spesso
            # due nomi giusti -- `Coda` e `fifo`, `Stack` e `pila` -- e tom ha
            # risposto `fifo` alle 21:40:32 su #cybernet trovando il silenzio.
            # Ignorare una risposta corretta e' peggio di non fare la domanda.
            out.append({"q": q, "a": a, "cat": d.get("cat", ""),
                        "alt": [x.strip() for x in d.get("alt", []) if x.strip()]})
    if not out:
        raise SystemExit("impiccato: catalogo vuoto")
    return out


# ------------------------------------------------------------------ stato

def empty_state():
    return {"scores": {}, "games": {}, "asked": []}


def load_state(path):
    """Lo stato tiene UNA partita per canale, non una per rete.

    Ordine di vjt (#sbiffo 2026-08-24 21:54): «abilitiamo il gioco anche su
    sniffo». Con la vecchia chiave `game` singola un `!impiccati` sul secondo
    canale sovrascriveva la partita viva del primo, che dal di la' si legge
    come un bot che si e' impiccato da solo. Le partite vecchie vengono
    migrate nel dizionario, non buttate: qualcuno potrebbe averla in corso.
    """
    if path.exists():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(d.get("scores"), dict):
                games = d.get("games")
                if not isinstance(games, dict):
                    games = {}
                    old = d.get("game")
                    if isinstance(old, dict) and old.get("chan"):
                        games[old["chan"]] = old
                d["games"] = games
                d.pop("game", None)
                d.setdefault("asked", [])
                return d
        except Exception:
            pass
    return empty_state()


def save_state(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ----------------------------------------------------------------- maschera

def mask_of(answer, revealed):
    """La risposta con le lettere non ancora scoperte sostituite da '-'.

    `revealed` contiene caratteri gia' normalizzati con fold(), cosi' una 'E'
    scoperta scopre anche la 'e' e la 'è'. Quello che si STAMPA e' pero'
    sempre il carattere originale della risposta, che e' come ShBot conserva
    'ReplicaSet' e 'Token JWT'.
    """
    out = []
    for ch in answer:
        if not is_hidden(ch):
            out.append(ch)
        elif fold(ch) in revealed:
            out.append(ch)
        else:
            out.append("-")
    return "".join(out)


def solved(answer, revealed):
    return all(not is_hidden(c) or fold(c) in revealed for c in answer)


def hidden_letters(answer, revealed):
    return sorted({fold(c) for c in answer if is_hidden(c) and fold(c) not in revealed})


MEDAGLIE = ("🥇", "🥈", "🥉")


def podio(rows, top=10):
    """Riga di punteggio col podio: medaglie ai primi tre, puntino agli altri.

    Stessa forma per il punteggio di partita e per la hall of fame (ordine di
    vjt, #sbiffo 16:25) — una funzione sola, cosi' non divergono al prossimo
    ritocco."""
    return " · ".join(
        f"{MEDAGLIE[i] if i < len(MEDAGLIE) else '·'} {r['nick']} {r['points']}"
        for i, r in enumerate(rows[:top]))


# -------------------------------------------------------------------- gioco

class Impiccato:
    def __init__(self, net, log, fifo, state_path, chans, domande, talk=True):
        self.net = net
        self.log = str(log)
        self.fifo = str(fifo) if fifo else ""
        self.state_path = state_path
        self.chans = chans
        self.domande = domande
        self.talk = talk
        self.state = load_state(state_path)
        self.sent = []          # solo per il selftest

    # --- uscita ---------------------------------------------------------

    def say(self, chan, text):
        self.sent.append((chan, text))
        if not self.talk or not self.fifo:
            return
        try:
            fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return          # nessun lettore sulla FIFO: si salta, non si appende
        try:
            os.write(fd, f"SAY {chan} {text}\n".encode("utf-8"))
        except OSError:
            pass
        finally:
            os.close(fd)

    # --- ciclo delle domande --------------------------------------------

    def pick(self, cats=None, exclude=()):
        """Una domanda del tema, MAI una gia' uscita in questa partita.

        Due memorie diverse, e vanno tenute separate. `exclude` sono le
        domande della partita in corso e non si perdona mai: ordine di vjt
        (DM 2026-08-25 16:07) «non dovrebbe ri-mostrare domande gia'
        presentate in una stessa partita» — prima l'unica memoria era quella
        lunga, che su un set corto si azzerava e faceva ricomparire la stessa
        domanda a meta' partita. `asked` invece e' la varieta' FRA partite, e
        li' il riciclo va bene: quando il tema e' esaurito si pesca lo stesso.

        Torna None quando il tema non ha piu' domande nuove per questa
        partita: li' la partita e' finita, non c'e' niente da riciclare.
        """
        tema = [d for d in self.domande if cats is None or d["cat"] in cats]
        if not tema:
            tema = self.domande
        seen = set(exclude)
        # Non basta escludere la DOMANDA: nel catalogo 202 domande su 667
        # condividono la risposta con un'altra ("Quale processo ha PID 1?" e
        # "Quale processo adotta gli orfani?" fanno entrambe `init`), e in un
        # gioco a maschera due domande con la stessa risposta SONO la stessa
        # domanda — stessa fila di trattini, stessa parola da scrivere.
        # Misurato in canale alle 16:17-16:18: `IRCv3` uscito due volte in
        # trenta secondi, a domande diverse.
        seen_a = {norm_answer(d["a"]) for d in self.domande if d["q"] in seen}
        fresh = [d for d in tema
                 if d["q"] not in seen and norm_answer(d["a"]) not in seen_a]
        if not fresh:
            return None
        asked = set(self.state.get("asked", []))
        pool = [d for d in fresh if d["q"] not in asked] or fresh
        return random.choice(pool)

    def game(self, chan):
        """La partita viva in QUESTO canale, o None. Ogni canale ha la sua."""
        return self.state["games"].get(chan)

    # --- set a tema ------------------------------------------------------

    def cats(self):
        """Le categorie che esistono davvero nel catalogo caricato."""
        return {d["cat"] for d in self.domande if d["cat"]}

    def resolve_set(self, name):
        """Nome del set -> insieme di categorie, o None se non esiste.

        `tutte` e' tutto, `nerd` e' NERD_CATS, `babbani` e' il complemento, e
        ogni singola categoria vale da sola come set."""
        key = fold(name).strip()
        key = SET_ALIASES.get(key, key)
        cats = self.cats()
        if key == "tutte":
            return cats
        if key == "nerd":
            got = cats & NERD_CATS
            return got or None
        if key == "babbani":
            got = cats - NERD_CATS
            return got or None
        match = {c for c in cats if fold(c) == key}
        return match or None

    def set_names(self):
        """I set da mostrare a chi scrive `!trivial` e basta."""
        cats = self.cats()
        out = ["tutte"]
        if cats & NERD_CATS:
            out.append("nerd")
        if cats - NERD_CATS:
            out.append("babbani")
        return out + sorted(cats)

    def elenco_set(self, chan):
        # Due righe e non una: col catalogo vero l'elenco dei set da solo vale
        # gia' 180 byte, e l'help attaccato in coda faceva 404 — oltre il
        # limite di riga, quindi troncato proprio dove c'e' scritto come si
        # gioca. Misurato, non stimato.
        self.say(chan, f"📚 Set: {' · '.join(self.set_names())}.")
        self.say(chan, f"🎯 Si parte con `!trivial <set>` — es. `!trivial babbani`. "
                       f"Partita da {DEFAULT_LIMIT} domande, `!trivial babbani 25` "
                       f"per cambiarne il numero. In gioco: `.h` una lettera, "
                       f"`.v` le vocali, `!stop` chiude, `!classifica` i punti.")

    def start_round(self, chan, setname="tutte", now=None, limit=None):
        """Apre una partita nuova nel canale e mette in gioco la prima domanda.

        `asked` e `count` vivono DENTRO la partita: sono la memoria che vjt ha
        chiesto, e muoiono con lei. Il limite si fissa qui e non si tocca piu':
        cambiarlo a meta' partita significa spostare il traguardo a chi sta
        gia' correndo.
        """
        self.state["games"][chan] = {
            "chan": chan,
            "set": setname,
            "limit": max(1, int(limit)) if limit else DEFAULT_LIMIT,
            "count": 0,
            "asked": [],
            "q": "",
            "a": "",
            "revealed": [],
            "alt": [],
            "last": int(now if now is not None else time.time()),
        }
        self.next_question(chan, now=now)

    def next_question(self, chan, now=None):
        """La domanda successiva, o la fine della partita.

        Due modi di finire, e vanno detti diversi: il limite raggiunto e' il
        traguardo, il set esaurito e' un catalogo troppo corto per il limite
        chiesto. Chi gioca deve capire quale dei due gli e' capitato.
        """
        g = self.state["games"][chan]
        limit = int(g.get("limit", DEFAULT_LIMIT))
        if int(g.get("count", 0)) >= limit:
            self.end_game(chan, reason="limite")
            return
        cats = self.resolve_set(g.get("set", "tutte")) or self.cats()
        d = self.pick(cats, exclude=g.get("asked", []))
        if d is None:
            self.end_game(chan, reason="esaurite")
            return
        g["q"], g["a"] = d["q"], d["a"]
        g["alt"], g["revealed"] = d.get("alt", []), []
        g["asked"] = list(g.get("asked", [])) + [d["q"]]
        g["count"] = int(g.get("count", 0)) + 1
        g["last"] = int(now if now is not None else time.time())
        # La memoria lunga resta troncata al catalogo: e' varieta' fra partite,
        # non un archivio.
        self.state["asked"] = (self.state.get("asked", []) + [d["q"]])[-len(self.domande):]
        self.say(chan, f"❓ [{g['count']}/{limit}] {d['q']}")
        self.say(chan, mask_of(d["a"], set()))

    def end_game(self, chan, reason="limite"):
        """Chiude la partita e stampa il punteggio DI QUESTA partita.

        La hall of fame resta su `!classifica`: a fine partita chi ha giocato
        vuole sapere chi ha vinto adesso, e la classifica generale — dove uno
        che gioca da una settimana e' irraggiungibile — se la mangia (ordine di
        vjt, #sbiffo 16:20).
        """
        g = self.state["games"].pop(chan, None)
        if not g:
            return
        n = int(g.get("count", 0))
        head = (f"🏁 Partita finita: {n} domande."
                if reason == "limite"
                else f"🏁 Finite le domande del set dopo {n}.")
        self.say(chan, f"{head} `!trivial <set> [n]` per un'altra.")
        rows = sorted(g.get("points", {}).values(),
                      key=lambda r: (-r["points"], r["nick"].casefold()))
        if not rows:
            self.say(chan, "😴 Punteggio partita: nessuno ha indovinato niente.")
            return
        self.say(chan, f"🏆 Punteggio partita: {podio(rows)} — "
                       f"`!classifica` per la hall of fame.")

    def stop_round(self, chan, reason="idle"):
        """reason: 'idle' = scaduta per silenzio, 'richiesto' = qualcuno ha
        chiesto !stop, 'quiet' = chiudi e non dire niente. Tenerle distinte
        conta: dire "nessuno risponde piu'" a chi ha appena scritto !stop e'
        una bugia in faccia a chi stava rispondendo eccome."""
        g = self.state["games"].pop(chan, None)
        if not g or reason == "quiet":
            return
        head = ("💤 Nessuno risponde piu'." if reason == "idle" else "🛑 Chiuso.")
        self.say(chan, f"{head} La risposta era: {g['a']}. "
                       f"`!trivial <set> [n]` per ripartire.")

    def win(self, chan, nick, now=None):
        g = self.state["games"][chan]
        answer = g["a"]
        key = canon_nick(nick).casefold()
        row = self.state["scores"].setdefault(key, {"nick": canon_nick(nick), "points": 0})
        row["nick"] = canon_nick(nick)
        row["points"] += 1
        # Punteggio DELLA partita, separato dalla hall of fame: a fine partita
        # interessa chi ha vinto stasera, non chi gioca da una settimana
        # (ordine di vjt, #sbiffo 16:20). Vive dentro `g`, quindi muore con lei.
        pts = g.setdefault("points", {})
        pts[key] = {"nick": canon_nick(nick), "points": int(pts.get(key, {}).get("points", 0)) + 1}
        self.say(chan, f"✅ Brava/o {canon_nick(nick)}, la risposta era: {answer}!!!")
        # La partita NON si ricrea a ogni vittoria: e' la stessa che va avanti,
        # e con lei il conteggio e l'elenco delle domande gia' uscite. Si resta
        # nel tema scelto all'inizio, che vive dentro `g`.
        self.next_question(chan, now=now)

    def reveal(self, chan, letters):
        """Scopre un insieme di lettere. True se almeno una era coperta."""
        g = self.state["games"][chan]
        rev = set(g["revealed"])
        want = {fold(c) for c in letters}
        present = {fold(c) for c in g["a"] if is_hidden(c)}
        new = (want & present) - rev
        if not new:
            return False
        rev |= new
        g["revealed"] = sorted(rev)
        return True

    def after_reveal(self, chan, nick, now=None):
        """Ripubblica la maschera e chiude il giro se e' venuta fuori intera."""
        g = self.state["games"][chan]
        if solved(g["a"], set(g["revealed"])):
            self.win(chan, nick, now=now)
        else:
            self.say(chan, mask_of(g["a"], set(g["revealed"])))

    def classifica(self, chan):
        rows = sorted(self.state["scores"].values(),
                      key=lambda r: (-r["points"], r["nick"].casefold()))
        if not rows:
            self.say(chan, "🗒️ Classifica vuota. `!trivial <set> [n]` per aprire le danze.")
            return
        self.say(chan, f"🏆 Classifica ({self.net}): {podio(rows)}")

    # --- ingresso --------------------------------------------------------

    def handle(self, nick, chan, text, now=None):
        """Una riga di canale. True se lo stato e' cambiato (il chiamante salva)."""
        if chan not in self.chans:
            return False
        text = text.strip()
        if not text:
            return False
        now = int(now if now is not None else time.time())

        if CLASSIFICA_PAT.match(text):
            self.classifica(chan)
            return False

        g = self.game(chan)

        # STOP prima di CMD: `!trivial stop` combacia anche con CMD_PAT, che
        # lo leggerebbe come il nome di un set e risponderebbe "sconosciuto"
        # a chi voleva solo chiudere.
        if STOP_PAT.match(text):
            if g:
                self.stop_round(chan, reason="richiesto")
                return True
            return False

        m = CMD_PAT.match(text)
        if m:
            want = (m.group("set") or "").strip()
            lim = m.group("limit")
            if g:
                # Partita gia' viva: si ripubblica e basta. Cambiare tema o
                # limite a meta' round butterebbe via la maschera di chi sta
                # giocando e sposterebbe il traguardo sotto i suoi piedi.
                g["last"] = now
                self.say(chan, f"❓ [{g.get('count', 1)}/{g.get('limit', DEFAULT_LIMIT)}] {g['q']}")
                self.say(chan, mask_of(g["a"], set(g["revealed"])))
                return True
            # `!trivial 50`: il lazy del gruppo `set` si mangia il numero se e'
            # l'unica cosa scritta, quindi il limite nudo arriva qui dentro
            # `want`, non dentro `lim`.
            if want.isdigit() and not lim:
                want, lim = "", want
            if not want and lim:
                # `!trivial 50`: il numero c'e' ma il set no. Dirlo, invece di
                # rispondere "set sconosciuto: 50" a chi ha solo scordato un
                # pezzo.
                self.say(chan, f"Il set va prima del numero: `!trivial <set> {lim}`. "
                               f"Ci sono: {' · '.join(self.set_names())}.")
                return False
            if not want:
                # Ordine di vjt: «si inizia un gioco solo scegliendo il set di
                # domande a tema». Niente set, niente partita: si elenca.
                self.elenco_set(chan)
                return False
            if self.resolve_set(want) is None:
                self.say(chan, f"🤨 Set sconosciuto: {want}. "
                               f"Ci sono: {' · '.join(self.set_names())}.")
                return False
            cats = self.resolve_set(want)
            n = sum(1 for d in self.domande if d["cat"] in cats)
            limit = max(1, int(lim)) if lim else DEFAULT_LIMIT
            # Il limite annunciato e' quello vero: se il set ha meno domande
            # del limite chiesto, la partita finira' li' e dirlo prima evita
            # che sembri un troncamento arbitrario.
            self.say(chan, f"🎲 Trivial, set {fold(want)}: {n} domande, "
                           f"partita da {min(limit, n)}. "
                           f"`.h` una lettera, `.v` le vocali, `!stop` chiude.")
            self.start_round(chan, setname=want, now=now, limit=limit)
            return True

        # Da qui in poi serve una partita viva in QUESTO canale.
        if not g:
            return False

        if VOWEL_PAT.match(text):
            g["last"] = now
            if self.reveal(chan, VOCALI):
                self.after_reveal(chan, nick, now=now)
            return True

        if HINT_PAT.match(text):
            g["last"] = now
            left = hidden_letters(g["a"], set(g["revealed"]))
            if left:
                self.reveal(chan, {random.choice(left)})
                self.after_reveal(chan, nick, now=now)
            return True

        # Lettera nuda: un solo carattere alfanumerico.
        if len(text) == 1 and is_hidden(text):
            g["last"] = now
            if self.reveal(chan, {text}):
                self.after_reveal(chan, nick, now=now)
            # Lettera assente: silenzio, come ShBot.
            return True

        # Tentativo intero.
        guess = norm_answer(text)
        if not guess:
            return False
        accepted = {norm_answer(g["a"])}
        accepted |= {norm_answer(x) for x in g.get("alt", [])}
        accepted.discard("")
        if guess in accepted:
            g["last"] = now
            # Vinta con un sinonimo: si scopre comunque tutto, cosi' la riga
            # finale mostra la risposta canonica e non la maschera monca.
            g["revealed"] = sorted({fold(c) for c in g["a"] if is_hidden(c)})
            self.win(chan, nick, now=now)
            return True

        # Parola giusta dentro la risposta: la si scopre, come fa ShBot.
        words = {norm_answer(w) for w in g["a"].split()}
        words.discard("")
        if guess in words:
            g["last"] = now
            target = next(w for w in g["a"].split() if norm_answer(w) == guess)
            if self.reveal(chan, {c for c in target if is_hidden(c)}):
                self.after_reveal(chan, nick, now=now)
                return True
        return False

    def tick(self, now=None):
        """Fa scadere le partite ferme da troppo, canale per canale.
        True se lo stato cambia."""
        now = int(now if now is not None else time.time())
        stale = [c for c, g in self.state["games"].items()
                 if now - int(g.get("last", now)) >= IDLE_STOP]
        for chan in stale:
            self.stop_round(chan)
        return bool(stale)

    # --- daemon ----------------------------------------------------------

    def run(self):
        p = subprocess.Popen(["tail", "-F", "-n", "0", self.log],
                             stdout=subprocess.PIPE, text=True, errors="replace")
        try:
            while True:
                r, _, _ = select.select([p.stdout], [], [], TICK)
                if r:
                    line = p.stdout.readline()
                    if not line:
                        break
                    m = PRIVMSG_PAT.search(line)
                    if m and self.handle(m.group("nick"), m.group("chan"),
                                         m.group("text")):
                        save_state(self.state_path, self.state)
                elif self.tick():
                    save_state(self.state_path, self.state)
        finally:
            p.terminate()


# ------------------------------------------------------------------- selftest

def selftest():
    """Esercita la meccanica senza toccare rete, FIFO o stato su disco."""
    import tempfile
    dom = [{"q": "Domanda di prova?", "a": "Token JWT", "cat": "t"}]
    with tempfile.TemporaryDirectory() as td:
        st = Path(td) / "s.json"
        g = Impiccato("test", "/dev/null", "", st, {"#t"}, dom, talk=False)

        g.handle("a", "#t", "!trivial t")
        assert g.sent[-1] == ("#t", "----- ---"), g.sent[-1]

        g.sent.clear()
        g.handle("a", "#t", "z")            # lettera assente -> silenzio
        assert g.sent == [], g.sent

        g.handle("a", "#t", "t")            # scopre tutte le T, case preservato
        assert g.sent[-1] == ("#t", "T---- --T"), g.sent[-1]

        g.sent.clear()
        g.handle("a", "#t", ".v")           # vocali
        assert g.sent[-1] == ("#t", "To-e- --T"), g.sent[-1]

        g.sent.clear()
        g.handle("b", "#t", "token")        # parola giusta -> la scopre
        assert g.sent[-1] == ("#t", "Token --T"), g.sent[-1]

        g.sent.clear()
        g.handle("b", "#t", "Token JWT")    # risposta intera -> vittoria
        assert g.sent[0] == ("#t", "✅ Brava/o b, la risposta era: Token JWT!!!"), g.sent[0]
        assert g.state["scores"]["b"]["points"] == 1
        # Catalogo di UNA domanda: non c'e' niente di nuovo da chiedere, quindi
        # la partita finisce invece di ripresentare la stessa.
        assert g.game("#t") is None, g.game("#t")
        assert "Finite le domande" in g.sent[1][1], g.sent[1]

        # sinonimo accettato: la risposta canonica esce comunque intera
        g6 = Impiccato("test", "/dev/null", "", st, {"#t"},
                       [{"q": "q", "a": "Coda", "alt": ["fifo"], "cat": "t"}], talk=False)
        g6.handle("a", "#t", "!trivial t")
        g6.sent.clear()
        g6.handle("c", "#t", "fifo")
        assert g6.sent[0] == ("#t", "✅ Brava/o c, la risposta era: Coda!!!"), g6.sent[0]
        assert g6.state["scores"]["c"]["points"] == 1

        # `.h` scopre una lettera e non due
        g2 = Impiccato("test", "/dev/null", "", st, {"#t"}, dom, talk=False)
        g2.handle("a", "#t", "!trivial t")
        g2.handle("a", "#t", ".h")
        assert len(g2.game("#t")["revealed"]) == 1, g2.game("#t")["revealed"]

        # accenti: 'e' scopre 'è'
        g3 = Impiccato("test", "/dev/null", "", st, {"#t"},
                       [{"q": "q", "a": "Perché", "cat": "t"}], talk=False)
        g3.handle("a", "#t", "!trivial t")
        g3.sent.clear()
        g3.handle("a", "#t", "e")
        # P-e-r-c-h-é: la 'e' nuda scopre sia la 'e' sia la 'é', e ognuna
        # viene ristampata col proprio accento.
        assert g3.sent[-1] == ("#t", "-e---é"), g3.sent[-1]

        # idle: la partita scade e lo dice
        g4 = Impiccato("test", "/dev/null", "", st, {"#t"}, dom, talk=False)
        g4.handle("a", "#t", "!trivial t", now=1000)
        assert g4.tick(now=1000 + IDLE_STOP) is True
        assert g4.game("#t") is None

        # canale sbagliato: non risponde
        g5 = Impiccato("test", "/dev/null", "", st, {"#t"}, dom, talk=False)
        assert g5.handle("a", "#altro", "!trivial t") is False
        assert g5.sent == []

        # due canali, due partite indipendenti: aprire la seconda non deve
        # ammazzare la prima (ordine di vjt: il gioco anche su #sniffo).
        g7 = Impiccato("test", "/dev/null", "", st, {"#uno", "#due"},
                       [{"q": "q", "a": "Coda", "cat": "t"}], talk=False)
        g7.handle("a", "#uno", "!trivial t", now=1000)
        g7.handle("a", "#uno", "c", now=1000)
        g7.handle("b", "#due", "!trivial t", now=1000)
        assert g7.game("#uno") and g7.game("#due"), g7.state["games"]
        assert g7.game("#uno")["revealed"] == ["c"], g7.game("#uno")
        assert g7.game("#due")["revealed"] == [], g7.game("#due")
        # una lettera su #due non tocca la maschera di #uno
        g7.sent.clear()
        g7.handle("b", "#due", "o", now=1000)
        assert g7.sent == [("#due", "-o--")], g7.sent
        # e chi vince su #due non chiude la partita di #uno
        g7.handle("b", "#due", "Coda", now=1000)
        assert g7.game("#uno")["revealed"] == ["c"], g7.game("#uno")
        assert g7.state["scores"]["b"]["points"] == 1
        # idle: scade solo il canale fermo. #due ha esaurito il catalogo con la
        # vittoria, quindi si riapre per avere due partite vive da confrontare.
        g7.handle("b", "#due", "!trivial t", now=1000 + IDLE_STOP)
        assert g7.game("#due") is not None, g7.state["games"]
        assert g7.tick(now=1000 + IDLE_STOP) is True
        assert g7.game("#uno") is None and g7.game("#due") is not None

        # --- set a tema (ordine di vjt, #sbiffo 21:57-21:59) ---
        mix = [{"q": "qn", "a": "Coda", "cat": "unix"},
               {"q": "qb", "a": "Scarpa", "cat": "moda"}]
        g8 = Impiccato("test", "/dev/null", "", st, {"#t"}, mix, talk=False)

        # `!trivial` nudo NON parte: elenca i set e basta.
        assert g8.handle("a", "#t", "!trivial") is False
        assert g8.game("#t") is None, "senza set non si gioca"
        # Due righe: l'elenco e poi l'help, che deve nominare limite e comandi.
        assert "Set:" in g8.sent[-2][1], g8.sent[-2]
        assert g8.sent[-2][1].startswith("📚"), g8.sent[-2]
        assert "babbani" in g8.sent[-2][1] and "nerd" in g8.sent[-2][1], g8.sent[-2]
        help_line = g8.sent[-1][1]
        assert str(DEFAULT_LIMIT) in help_line, help_line
        for tok in ("!trivial babbani 25", ".h", ".v", "!stop", "!classifica"):
            assert tok in help_line, (tok, help_line)
        # Nessuna delle due deve sforare la riga IRC.
        for _, t in g8.sent[-2:]:
            assert len(t.encode()) <= 400, (len(t.encode()), t)

        # set sconosciuto: lo dice, non parte.
        g8.sent.clear()
        assert g8.handle("a", "#t", "!trivial pokemon") is False
        assert g8.game("#t") is None
        assert "sconosciuto" in g8.sent[-1][1], g8.sent[-1]

        # `babbani` = complemento di NERD_CATS: qui pesca solo la moda.
        g8.sent.clear()
        assert g8.handle("a", "#t", "!trivial babbani") is True
        assert g8.game("#t")["a"] == "Scarpa", g8.game("#t")
        assert g8.sent[-1] == ("#t", "------"), g8.sent[-1]

        # il tema regge fra una domanda e l'altra: con due domande di moda,
        # dopo la vittoria la partita resta di la' e non sborda nel nerd.
        moda2 = mix + [{"q": "qb2", "a": "Cappello", "cat": "moda"}]
        g8b = Impiccato("test", "/dev/null", "", st, {"#t"}, moda2, talk=False)
        g8b.handle("a", "#t", "!trivial babbani")
        prima = g8b.game("#t")["a"]
        g8b.handle("b", "#t", prima)
        assert g8b.game("#t")["set"] == "babbani", g8b.game("#t")
        assert g8b.game("#t")["a"] in {"Scarpa", "Cappello"}, g8b.game("#t")
        assert g8b.game("#t")["a"] != prima, "domanda ripetuta nella stessa partita"
        assert g8b.game("#t")["count"] == 2, g8b.game("#t")

        # `nerd` pesca solo dall'altra sponda, `tutte` da entrambe.
        g9 = Impiccato("test", "/dev/null", "", st, {"#t"}, mix, talk=False)
        g9.handle("a", "#t", "!trivial nerd")
        assert g9.game("#t")["a"] == "Coda", g9.game("#t")
        assert g9.resolve_set("tutte") == {"unix", "moda"}
        assert g9.resolve_set("bestemmie") is None, "alias verso una cat assente = None"
        assert g9.resolve_set("scarpe") == {"moda"}, "alias scarpe -> moda"

        # `!trivial stop` chiude, non viene letto come nome di un set.
        g9.sent.clear()
        assert g9.handle("a", "#t", "!trivial stop") is True
        assert g9.game("#t") is None
        assert "🛑 Chiuso." in g9.sent[0][1], g9.sent[0]

        # `!impiccati <set>` resta valido: chi giocava su #cybernet non si
        # trova il bot muto.
        g10 = Impiccato("test", "/dev/null", "", st, {"#t"}, mix, talk=False)
        assert g10.handle("a", "#t", "!impiccati nerd") is True
        assert g10.game("#t")["a"] == "Coda"

        # --- niente ripetizioni + limite di partita (vjt, DM 2026-08-25 16:07) ---
        many = [{"q": f"q{i}", "a": f"Risposta{i}", "cat": "t"} for i in range(40)]

        # Nessuna domanda esce due volte nella stessa partita, e la partita si
        # chiude da sola all'ultimo giro senza che nessuno dica `!stop`.
        g11 = Impiccato("test", "/dev/null", "", st, {"#t"}, many, talk=False)
        g11.handle("a", "#t", "!trivial t")
        assert g11.game("#t")["limit"] == DEFAULT_LIMIT, g11.game("#t")
        viste = []
        for _ in range(DEFAULT_LIMIT):
            gg = g11.game("#t")
            assert gg is not None, f"partita chiusa dopo {len(viste)}"
            viste.append(gg["q"])
            g11.handle("b", "#t", gg["a"])
        assert len(set(viste)) == DEFAULT_LIMIT, "domanda ripetuta nella partita"
        assert g11.game("#t") is None, "la partita deve chiudersi al limite"
        assert any(f"Partita finita: {DEFAULT_LIMIT} domande." in t
                   for _, t in g11.sent[-3:]), g11.sent[-3:]

        # Due domande diverse con la STESSA risposta valgono per una sola: la
        # maschera e la parola da scrivere sono identiche, quindi al giocatore
        # sembra la domanda ripetuta (visto in canale: `IRCv3` due volte in 30
        # secondi, vjt «non dobbiamo scegliere domande con la stessa risposta»).
        gemelle = [{"q": "q1", "a": "Init", "cat": "t"},
                   {"q": "q2", "a": "init", "cat": "t"},
                   {"q": "q3", "a": "Fork", "cat": "t"}]
        g11b = Impiccato("test", "/dev/null", "", st, {"#t"}, gemelle, talk=False)
        g11b.handle("a", "#t", "!trivial t 3")
        risposte = []
        while g11b.game("#t"):
            risposte.append(norm_answer(g11b.game("#t")["a"]))
            g11b.handle("b", "#t", g11b.game("#t")["a"])
        assert len(risposte) == 2, risposte
        assert len(set(risposte)) == 2, risposte

        # Limite esplicito a inizio partita: `!trivial t 3`.
        g12 = Impiccato("test", "/dev/null", "", st, {"#t"}, many, talk=False)
        assert g12.handle("a", "#t", "!trivial t 3") is True
        assert g12.game("#t")["limit"] == 3, g12.game("#t")
        assert g12.game("#t")["set"] == "t", "il numero non deve finire nel set"
        for _ in range(3):
            g12.handle("b", "#t", g12.game("#t")["a"])
        assert g12.game("#t") is None, "il limite chiesto deve valere"

        # Punteggio DI PARTITA a fine giro, non la hall of fame (vjt 16:20).
        # `b` arriva alla seconda partita con 3 punti gia' in classifica
        # generale: quelli non devono comparire nel riepilogo della partita.
        g12b = Impiccato("test", "/dev/null", "", st, {"#t"}, many, talk=False)
        for _ in range(2):                      # prima partita, 2 punti a `b`
            g12b.handle("a", "#t", "!trivial t 1")
            g12b.handle("b", "#t", g12b.game("#t")["a"])
        g12b.handle("a", "#t", "!trivial t 3")  # terza partita: 2 a `c`, 1 a `d`
        g12b.handle("c", "#t", g12b.game("#t")["a"])
        g12b.handle("c", "#t", g12b.game("#t")["a"])
        g12b.sent.clear()
        g12b.handle("d", "#t", g12b.game("#t")["a"])
        fine = [t for _, t in g12b.sent if "Punteggio partita" in t]
        assert len(fine) == 1, g12b.sent
        assert "🥇 c 2" in fine[0] and "🥈 d 1" in fine[0], fine[0]
        assert " b " not in fine[0], ("la hall of fame non entra qui", fine[0])
        assert g12b.state["scores"]["b"]["points"] == 2, g12b.state["scores"]

        # Il limite non si sposta a partita in corso: `!trivial t 99` ripubblica.
        g13 = Impiccato("test", "/dev/null", "", st, {"#t"}, many, talk=False)
        g13.handle("a", "#t", "!trivial t 5")
        g13.handle("a", "#t", "!trivial t 99")
        assert g13.game("#t")["limit"] == 5, g13.game("#t")

        # Set piu' corto del limite: finisce le domande e lo dice diverso.
        g14 = Impiccato("test", "/dev/null", "", st, {"#t"}, many[:2], talk=False)
        g14.handle("a", "#t", "!trivial t 10")
        for _ in range(2):
            g14.handle("b", "#t", g14.game("#t")["a"])
        assert g14.game("#t") is None
        assert any("Finite le domande del set dopo 2." in t for _, t in g14.sent[-3:]), g14.sent[-3:]

        # Numero senza set: lo dice invece di rispondere "set sconosciuto: 50".
        g15 = Impiccato("test", "/dev/null", "", st, {"#t"}, many, talk=False)
        assert g15.handle("a", "#t", "!trivial 50") is False
        assert g15.game("#t") is None
        assert "prima del numero" in g15.sent[-1][1], g15.sent[-1]

        # migrazione: la vecchia chiave `game` singola finisce nel dizionario
        old = Path(td) / "old.json"
        old.write_text(json.dumps({"scores": {}, "asked": [],
                                   "game": {"chan": "#x", "q": "q", "a": "Coda",
                                            "revealed": [], "alt": [], "last": 1}}),
                       encoding="utf-8")
        assert load_state(old)["games"]["#x"]["a"] == "Coda"

    print("selftest ok")


# ----------------------------------------------------------------------- main

def build(net, talk=True):
    cfg = NETS[net]
    chans = os.environ.get("IMPICCATO_CHANS")
    return Impiccato(
        net,
        os.environ.get("IMPICCATO_LOG", cfg["log"]),
        os.environ.get("IMPICCATO_FIFO", cfg["fifo"]),
        Path(os.environ.get("IMPICCATO_STATE", cfg["state"])),
        {c.strip() for c in chans.split(",")} if chans else cfg["chans"],
        load_domande(),
        talk=talk,
    )


def main():
    argv = sys.argv[1:]
    net = "azzurra"
    if "--net" in argv:
        i = argv.index("--net")
        net = argv[i + 1]
        del argv[i:i + 2]
    if net not in NETS:
        raise SystemExit(f"impiccato: rete sconosciuta {net!r} (usa: {', '.join(NETS)})")

    cmd = argv[0] if argv else "daemon"
    if cmd == "selftest":
        selftest()
    elif cmd == "classifica":
        g = build(net, talk=False)
        rows = sorted(g.state["scores"].values(),
                      key=lambda r: (-r["points"], r["nick"].casefold()))
        if not rows:
            print("(nessun punto)")
        for i, r in enumerate(rows):
            medaglia = MEDAGLIE[i] if i < len(MEDAGLIE) else " ·"
            print(f"  {medaglia} {r['nick']:<20} {r['points']}")
    else:
        build(net).run()


if __name__ == "__main__":
    main()
