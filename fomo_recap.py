# -*- coding: utf-8 -*-
"""
Récap Fomo du matin
===================

Ce script NE PASSE AUCUN ORDRE. Il lit les trades publics faits via l'app
Fomo sur la blockchain Solana (API Bitquery), garde l'historique sur ton PC,
repère les traders qui ont l'air vraiment bons (et écarte les bots / faux
gains), puis t'envoie sur Telegram les tokens qu'ils viennent d'acheter.

Il suit aussi ses propres propositions (sans argent) pour qu'on sache, après
2-3 semaines, si ça aurait vraiment rapporté.

Commandes (à lancer avec run.bat, ex. :  run.bat recap ) :
  check        teste la config : Bitquery + envoi d'un message Telegram
  telegram-id  affiche ton chat_id Telegram (après avoir écrit à ton bot)
  collect      récupère les derniers trades Fomo (à lancer toutes les 2 h)
  recap        calcule et envoie le récap (le matin, ou quand tu veux)
  recap-test   pareil mais affiche le récap ici sans l'envoyer
  cloud        une passe complète sur GitHub Actions (collecte, backtest, alertes, récap)
  cloud-seed   prépare la base de départ du cloud (sans ton wallet ni ton journal)
  cloud-pull   récupère tout de suite les données du cloud
  sample       affiche quelques trades bruts (pour vérifier les données)

Uniquement la bibliothèque standard de Python : rien à installer.
"""

import json
import math
import random
import os
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
DB_PATH = os.path.join(HERE, "fomo_data.sqlite")

# Monnaies "de paiement" : si l'utilisateur reçoit l'une d'elles, c'est une vente.
QUOTE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "So11111111111111111111111111111111111111112",   # SOL (wrapped)
    "11111111111111111111111111111111",              # SOL natif
}
QUOTE_SYMBOLS = {"USDC", "USDT", "SOL", "WSOL", "USDG", "USD1", "PYUSD"}

DEFAULT_CONFIG = {
    "bitquery_token": "COLLE_ICI_TON_JETON_BITQUERY",
    "telegram_bot_token": "COLLE_ICI_LE_JETON_DE_TON_BOT_TELEGRAM",
    "telegram_chat_id": "",
    "bitquery_endpoint": "https://streaming.bitquery.io/graphql",
    # Source des données : "solana" (gratuit, nœud public) ou "bitquery" (payant après l'essai)
    "data_source": "solana",
    "solana_rpc": "https://api.mainnet-beta.solana.com",
    "rpc_min_interval": 1.1,      # secondes entre deux requêtes (le nœud public bloque si on va trop vite)
    "rpc_max_requests": 750,      # par collecte (environ 14 min)
    "discovery_txs": 120,         # trades Fomo échantillonnés à chaque collecte
    "discovery_min_usd": 10,
    "max_tracked_wallets": 150,
    "backfill_txs": 30,           # historique lu la 1re fois qu'on suit un wallet
    "max_new_wallets_per_run": 15,
    "max_txs_per_wallet": 40,
    "backtest_days": 30,          # fenêtre du backtest
    "min_retained": 20,           # en dessous : critères assouplis automatiquement (un cran)
    "no_green_days": 14,          # sans aucun feu vert pendant ce temps : diagnostic "trop strict"
    "backtest_target": 40,        # nb de créneaux mesurés (à +24 h) pour un résultat fiable (~2 semaines)
    "fomo_token_url": "",         # modèle de lien Fomo vers un token, avec {mint} (si tu trouves le format)
    "my_wallet": "",              # adresse publique de TON wallet Fomo (lecture seule)
    # Adresse qui signe tous les trades Fomo (identifiée par Bitquery, pas par Fomo).
    "fomo_signer": "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51",
    "fomo_protocol": "dflow",
    # Collecte
    "min_trade_usd": 50,          # ignore les trades plus petits (économise le quota Bitquery)
    "rows_per_page": 2000,
    "max_pages_per_collect": 5,
    # Tri des traders
    "history_days": 10,           # historique utilisé pour noter les traders
    "min_closed_positions": 5,    # nb minimum de positions fermées pour être noté
    "min_win_rate": 0.5,
    "min_profit_factor": 1.5,
    "min_median_hold_minutes": 5, # en dessous : sniper/bot, impossible à copier
    "max_trades_per_day": 150,    # au dessus : bot
    "max_free_sell_ratio": 0.4,   # part des ventes de tokens jamais achetés (initiés / airdrops)
    "max_top_token_profit_share": 0.7,  # gain venant d'un seul token : coup de chance
    # Candidats du matin
    "fresh_buy_hours": 16,        # achats récents pris en compte
    "max_runup": 1.5,             # prix déjà x1.5 depuis leur achat : trop tard
    "max_candidates": 3,
    "assumed_costs_pct": 3.0,     # frais + glissement estimés aller-retour, pour le suivi
    # Signal du moment
    "signal_min_history_days": 3,
    "signal_min_track_days": 7,
    "alert_on_green": True,       # alerte Telegram quand un feu vert apparaît
    "app_auto_collect_minutes": 30,  # collecte auto tant que l'appli est ouverte (0 = jamais)
    # Sécurité du token (DexScreener + RugCheck, gratuits)
    "min_liquidity_usd": 20000,   # en dessous : trop facile à manipuler, revente difficile
    "market_cache_minutes": 10,
    "safety_cache_hours": 6,
    # Sortie
    "stop_loss_pct": -25,         # alerte "sortir" si la position perd plus que ça
    "take_profit_pct": 50,        # alerte "prendre ses gains" au-delà
    "alert_on_exit": True,
    # Cloud GitHub (collecte 24 h/24 même PC éteint)
    "cloud_repo": "",             # ex. "pseudo/fomo-recap-cloud" ; vide = pas de cloud
    "cloud_sync_minutes": 10,     # l'appli récupère les données du cloud toutes les X minutes
    "cloud_stale_hours": 6,       # si le cloud ne donne plus signe de vie, le PC reprend la main
    "recap_hour": 8,              # heure du récap du matin (heure de Paris), envoyé par le cloud
}


# --------------------------------------------------------------------------
# Config / base de données
# --------------------------------------------------------------------------

def cloud_env_config():
    """Réglages quand le script tourne sur GitHub Actions : les clés viennent des « secrets »
    du dépôt (jamais écrites dans un fichier), le reste de cloud_config.json."""
    merged = dict(DEFAULT_CONFIG)
    p = os.path.join(HERE, "cloud_config.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            merged.update(json.load(f))
    merged["telegram_bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    merged["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    merged["my_wallet"] = ""  # ton wallet n'est jamais suivi dans le cloud (dépôt public)
    merged["hide_wallets"] = [w.strip() for w in os.environ.get("HIDE_WALLET", "").split(",") if w.strip()]
    merged["data_source"] = "solana"
    merged["cloud_repo"] = ""
    merged["_cloud"] = True
    return merged


def load_config():
    if os.environ.get("FOMO_CLOUD"):
        return cloud_env_config()
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print("J'ai créé config.json : remplis tes jetons dedans puis relance.")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    con.execute("""CREATE TABLE IF NOT EXISTS trades(
        sig TEXT, ts REAL, wallet TEXT, side TEXT, mint TEXT, symbol TEXT,
        token_amount REAL, usd REAL, price REAL,
        PRIMARY KEY(sig, mint, side))""")
    con.execute("CREATE INDEX IF NOT EXISTS i_wallet ON trades(wallet)")
    con.execute("CREATE INDEX IF NOT EXISTS i_mint ON trades(mint, ts)")
    con.execute("CREATE INDEX IF NOT EXISTS i_ts ON trades(ts)")
    con.execute("""CREATE TABLE IF NOT EXISTS picks(
        day TEXT, rank INTEGER, mint TEXT, symbol TEXT, price REAL, score REAL,
        PRIMARY KEY(day, mint))""")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return con


def meta_get(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    con.commit()


# --------------------------------------------------------------------------
# Bitquery
# --------------------------------------------------------------------------

TRADES_QUERY = """
query ($signer: String!, $protocol: String!, $since: DateTime!, $limit: Int!, $minusd: String!) {
  Solana {
    DEXTrades(
      limit: {count: $limit}
      orderBy: {ascending: Block_Time}
      where: {
        Block: {Time: {after: $since}}
        Transaction: {Signer: {is: $signer}, Result: {Success: true}}
        Trade: {Dex: {ProtocolName: {is: $protocol}}, Buy: {AmountInUSD: {ge: $minusd}}}
      }
    ) {
      Block { Time }
      Transaction { Signature }
      Trade {
        Buy {
          Amount AmountInUSD PriceInUSD
          Account { Owner }
          Currency { MintAddress Symbol }
        }
        Sell {
          Amount AmountInUSD PriceInUSD
          Account { Owner }
          Currency { MintAddress Symbol }
        }
      }
    }
  }
}
"""


def bitquery(cfg, query, variables):
    token = cfg["bitquery_token"].strip()
    body = json.dumps({"query": query, "variables": variables}).encode()
    last_err = None
    # 1er essai : en-tête Bearer (méthode officielle) ; 2e essai : ?token= dans l'URL
    for url, headers in (
        (cfg["bitquery_endpoint"], {"Authorization": "Bearer " + token}),
        (cfg["bitquery_endpoint"] + "?token=" + urllib.parse.quote(token), {}),
    ):
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            data = None
            for attempt in range(3):  # Bitquery coupe parfois la connexion : on réessaie
                try:
                    with urllib.request.urlopen(req, timeout=90) as r:
                        data = json.loads(r.read().decode())
                    break
                except urllib.error.HTTPError:
                    raise
                except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                    if attempt == 2:
                        raise RuntimeError(f"Bitquery ne répond pas ({e}). Réessaie dans quelques minutes.")
                    time.sleep(5 * (attempt + 1))
            break
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code != 401:
                raise RuntimeError(f"Bitquery HTTP {e.code} : {e.read().decode()[:300]}")
    else:
        msg = "Bitquery HTTP 401 : jeton refusé."
        if not token.startswith("ory_at_"):
            msg += (" Ce n'est pas le bon type de jeton : il faut un 'Access Token' V2 qui commence"
                    " par ory_at_ (https://account.bitquery.io/user/api_v2/access_tokens).")
        else:
            msg += (" Vérifie qu'il est copié en entier, sans espace, et qu'il n'a pas expiré"
                    " (regénère-en un sur https://account.bitquery.io/user/api_v2/access_tokens).")
        raise RuntimeError(msg)
    if data.get("errors"):
        raise RuntimeError("Bitquery a répondu une erreur : " + json.dumps(data["errors"])[:800])
    return data["data"]


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def is_quote(mint, symbol):
    return mint in QUOTE_MINTS or (symbol or "").upper() in QUOTE_SYMBOLS


def parse_trade(row):
    """Transforme une ligne Bitquery en trade du point de vue de l'utilisateur Fomo.
    L'utilisateur reçoit 'Buy' et donne 'Sell'."""
    t = row["Trade"]
    b, s = t["Buy"], t["Sell"]
    bm, bs = b["Currency"]["MintAddress"], b["Currency"]["Symbol"]
    sm, ss = s["Currency"]["MintAddress"], s["Currency"]["Symbol"]
    wallet = (b.get("Account") or {}).get("Owner")
    if not wallet:
        return None
    ts = datetime.fromisoformat(row["Block"]["Time"].replace("Z", "+00:00")).timestamp()
    sig = row["Transaction"]["Signature"]
    usd = f(b["AmountInUSD"]) or f(s["AmountInUSD"])
    if is_quote(bm, bs) and not is_quote(sm, ss):
        # reçoit USDC/SOL, donne le token : VENTE du token
        amt = f(s["Amount"])
        price = f(s["PriceInUSD"]) or (usd / amt if amt else 0)
        return (sig, ts, wallet, "sell", sm, ss, amt, usd, price)
    if not is_quote(bm, bs):
        # reçoit le token : ACHAT
        amt = f(b["Amount"])
        price = f(b["PriceInUSD"]) or (usd / amt if amt else 0)
        return (sig, ts, wallet, "buy", bm, bs, amt, usd, price)
    return None  # échange entre deux monnaies de paiement : ignoré



# --------------------------------------------------------------------------
# Source gratuite : nœud public Solana (aucune clé, aucun compte)
# --------------------------------------------------------------------------

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KgNaCKZqTfK4o4xbZdTL"  # rarement utilisé ici
WSOL = "So11111111111111111111111111111111111111112"


class BudgetDone(Exception):
    pass


class Rpc:
    """Appels JSON-RPC lents et polis (le nœud public limite le débit)."""

    def __init__(self, url, min_interval=1.1, max_requests=450):
        self.url, self.min_interval, self.max_requests = url, min_interval, max_requests
        self.count, self.last = 0, 0.0

    def call(self, method, params):
        if self.count >= self.max_requests:
            raise BudgetDone()
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        for attempt in range(6):
            wait = self.min_interval - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.count += 1
            req = urllib.request.Request(self.url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": "FomoRecap/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    d = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Nœud Solana : HTTP {e.code}")
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                if attempt < 5:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Nœud Solana injoignable ({e})")
            if d.get("error"):
                err = d["error"]
                if isinstance(err, dict) and err.get("code") in (-32005, -32429) and attempt < 5:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError("Nœud Solana : " + json.dumps(err)[:200])
            return d.get("result")
        raise RuntimeError("Nœud Solana : trop de refus, réessaie plus tard")

    def tx(self, sig):
        return self.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])

    def sigs(self, address, limit, until=None):
        opts = {"limit": limit}
        if until:
            opts["until"] = until
        return self.call("getSignaturesForAddress", [address, opts]) or []


def parse_swap(tx, fomo_signer, sol_price, wallet=None):
    """Transforme une transaction en achat/vente pour le wallet de l'utilisateur, sinon None."""
    if not tx or not tx.get("meta") or tx["meta"].get("err"):
        return None
    msg = tx["transaction"]["message"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]
    signers = [k["pubkey"] for k in msg["accountKeys"] if isinstance(k, dict) and k.get("signer")]
    meta = tx["meta"]
    if wallet is None:
        users = [x for x in signers if x != fomo_signer] or signers[:1]
        if not users:
            return None
        wallet = users[0]
    deltas = {}
    for side, arr in ((-1, meta.get("preTokenBalances") or []), (1, meta.get("postTokenBalances") or [])):
        for b in arr:
            if b.get("owner") != wallet:
                continue
            v = f((b.get("uiTokenAmount") or {}).get("uiAmountString"))
            deltas[b["mint"]] = deltas.get(b["mint"], 0.0) + side * v
    sol = deltas.pop(WSOL, 0.0)
    if wallet in keys:
        i = keys.index(wallet)
        if i < len(meta.get("preBalances", [])):
            lam = meta["postBalances"][i] - meta["preBalances"][i]
            if i == 0:
                lam += meta.get("fee") or 0  # le trader paie lui-même les frais : on les retire
            sol += lam / 1e9
    quote_usd = 0.0
    q = deltas.pop(USDC, 0.0) + deltas.pop(USDT, 0.0)
    if abs(q) > 1e-6:
        quote_usd = q
    elif abs(sol) > 0.003 and sol_price:
        quote_usd = sol * sol_price
    tokens = {m: d for m, d in deltas.items() if abs(d) > 1e-12}
    if len(tokens) != 1 or abs(quote_usd) < 0.5:
        return None
    mint, amt = next(iter(tokens.items()))
    if amt > 0 and quote_usd < 0:
        side = "buy"
    elif amt < 0 and quote_usd > 0:
        side = "sell"
    else:
        return None
    usd, amt = abs(quote_usd), abs(amt)
    sig = tx["transaction"]["signatures"][0]
    return (sig, float(tx.get("blockTime") or time.time()), wallet, side, mint, None, amt, usd, usd / amt)


def watch_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS watch(
        wallet TEXT PRIMARY KEY, added REAL, last_sig TEXT, last_check REAL,
        seen INTEGER DEFAULT 0, usd REAL DEFAULT 0, backfilled INTEGER DEFAULT 0)""")


def sol_usd(cfg, con):
    try:
        fetch_market(cfg, con, [WSOL])
    except Exception:
        pass
    mk = market_info(con, WSOL)
    return mk["price"] if mk and mk["price"] else None


def resolve_symbols(cfg, con, limit=90):
    ext_tables(con)
    mints = [r[0] for r in con.execute(
        "SELECT DISTINCT t.mint FROM trades t LEFT JOIN market m ON m.mint=t.mint "
        "WHERE t.symbol IS NULL AND (m.mint IS NULL OR m.sym IS NULL) LIMIT ?", (limit,))]
    if mints:
        try:
            fetch_market(cfg, con, mints)
        except Exception:
            pass
    con.execute("UPDATE trades SET symbol=(SELECT sym FROM market WHERE market.mint=trades.mint) "
                "WHERE symbol IS NULL AND EXISTS(SELECT 1 FROM market WHERE market.mint=trades.mint AND sym IS NOT NULL)")
    con.execute("UPDATE trades SET symbol=substr(mint,1,4)||'…' WHERE symbol IS NULL AND ts < ?", (time.time() - 3600,))
    con.commit()


def collect_solana(cfg, con, verbose=True, only_mine=False):
    """only_mine=True : ne lit que ton wallet (le PC le fait quand le cloud collecte le reste)."""
    mine = (cfg.get("my_wallet") or "").strip()
    if only_mine and not mine:
        return 0
    hide = set(cfg.get("hide_wallets") or [])
    lock = float(meta_get(con, "collect_lock", 0) or 0)
    if time.time() - lock < 15 * 60:
        if verbose:
            print("Une collecte est déjà en cours (ou vient de finir) : on passe.")
        return 0
    meta_set(con, "collect_lock", time.time())
    watch_table(con)
    rpc = Rpc(cfg["solana_rpc"], cfg["rpc_min_interval"], cfg["rpc_max_requests"])
    signer = cfg["fomo_signer"]
    price_sol = sol_usd(cfg, con)
    added = 0

    def store(t):
        nonlocal added
        if t is None or t[2] in hide:
            return
        cur = con.execute("INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?,?)", t)
        added += cur.rowcount
        con.commit()

    if hide:
        for w in hide:
            con.execute("DELETE FROM trades WHERE wallet=?", (w,))
            con.execute("DELETE FROM watch WHERE wallet=?", (w,))
        con.commit()

    try:
        # 1) Découverte : un échantillon des derniers trades Fomo, pour repérer des traders actifs
        sigs = [] if only_mine else [x for x in rpc.sigs(signer, 1000) if not x.get("err")]
        for x in random.sample(sigs, min(len(sigs), int(cfg["discovery_txs"]))):
            t = parse_swap(rpc.tx(x["signature"]), signer, price_sol)
            store(t)
            if t and t[7] >= cfg["discovery_min_usd"] and t[2] not in hide:
                con.execute("INSERT OR IGNORE INTO watch(wallet, added) VALUES(?,?)", (t[2], time.time()))
                con.execute("UPDATE watch SET seen=seen+1, usd=usd+? WHERE wallet=?", (t[7], t[2]))
                con.commit()
        con.commit()

        # 2) Suivi : l'historique des meilleurs wallets (bons traders d'abord, puis les plus actifs)
        stats, good = ({}, {}) if only_mine else build_recap(cfg, con, limit=1)[:2]
        # Actualisation : les wallets suivis qui ont assez d'historique mais ne passent plus le tri
        # (perdants, bots, coups de chance) laissent leur place à de nouveaux traders.
        for w, st in stats.items():
            if w != mine and w not in good and st.get("closed", 0) >= cfg["min_closed_positions"]:
                con.execute("DELETE FROM watch WHERE wallet=? AND backfilled=1", (w,))
        if mine:
            con.execute("INSERT OR IGNORE INTO watch(wallet, added) VALUES(?,?)", (mine, time.time()))
        con.commit()
        if not only_mine:
            snapshot_ranks(con, good)
        rows = con.execute("SELECT wallet, last_sig, backfilled, last_check, usd FROM watch").fetchall()
        if only_mine:
            rows = [r for r in rows if r[0] == mine]
        rows.sort(key=lambda r: (0 if r[0] == mine else 1 if r[0] in good else 2,
                                 -(good.get(r[0]) or 0), -(r[4] or 0)))
        tracked = rows[: int(cfg["max_tracked_wallets"])]
        tracked.sort(key=lambda r: (0 if r[0] == mine else 1 if r[0] in good else 2, r[3] or 0))
        new_wallets = 0
        for w, last_sig, backfilled, _, _ in tracked:
            allsig = []
            if not backfilled:
                if new_wallets >= cfg["max_new_wallets_per_run"] and w != mine:
                    continue
                new_wallets += 1
                lst = [x for x in rpc.sigs(w, 100 if w == mine else int(cfg["backfill_txs"])) if not x.get("err")]
            else:
                # Rattrapage : tout ce que le wallet a fait depuis la dernière lecture, même si le PC
                # était éteint entre-temps (la blockchain garde tout). On lit du plus ancien au plus récent,
                # par paquets, et on reprend là où on s'est arrêté à la collecte suivante.
                allsig = rpc.sigs(w, 1000, until=last_sig)
                ok = [x for x in allsig if not x.get("err")]
                cap = 100 if w == mine else int(cfg["max_txs_per_wallet"])
                if len(ok) > cap:
                    chunk = ok[-cap:]
                    for x in reversed(chunk):
                        store(parse_swap(rpc.tx(x["signature"]), signer, price_sol, wallet=w))
                    con.execute("UPDATE watch SET last_check=?, last_sig=? WHERE wallet=?",
                                (time.time() - 3600, chunk[0]["signature"], w))  # repassera vite pour finir
                    con.commit()
                    continue
                lst = ok
            for x in lst[: 100 if w == mine else int(cfg["max_txs_per_wallet"])]:
                store(parse_swap(rpc.tx(x["signature"]), signer, price_sol, wallet=w))
            newest = (allsig[0]["signature"] if (backfilled and allsig) else (lst[0]["signature"] if lst else None))
            con.execute("UPDATE watch SET last_check=?, backfilled=1, last_sig=COALESCE(?, last_sig) WHERE wallet=?",
                        (time.time(), newest, w))
            con.commit()
    except BudgetDone:
        pass
    finally:
        con.commit()
        meta_set(con, "collect_lock", 0)

    # 3) Noms des tokens, ménage
    resolve_symbols(cfg, con)
    con.execute("DELETE FROM watch WHERE added < ? AND seen <= 1 AND backfilled=1 AND wallet NOT IN "
                "(SELECT DISTINCT wallet FROM trades WHERE ts > ?)", (time.time() - 3 * 86400, time.time() - 3 * 86400))
    con.execute("DELETE FROM trades WHERE ts < ?", (time.time() - 30 * 86400,))
    con.commit()
    if only_mine:
        meta_set(con, "mine_last_run", time.time())
        return added
    newest = con.execute("SELECT MAX(ts) FROM trades").fetchone()[0]
    if newest:
        meta_set(con, "last_ts", newest)
    meta_set(con, "last_collect_run", time.time())
    meta_set(con, "last_rpc_requests", rpc.count)
    if verbose:
        n = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        nw = con.execute("SELECT COUNT(*) FROM watch").fetchone()[0]
        print(f"Collecte OK (nœud Solana gratuit) : {added} trades ajoutés, {n} en base, "
              f"{nw} wallets suivis, {rpc.count} requêtes.")
    return added


def collect(cfg, con, verbose=True):
    if cfg.get("data_source", "solana") == "bitquery":
        return collect_bitquery(cfg, con, verbose)
    return collect_solana(cfg, con, verbose)


def collect_bitquery(cfg, con, verbose=True):
    last = meta_get(con, "last_ts")
    floor = datetime.now(timezone.utc) - timedelta(hours=11.5)  # Bitquery garde ~12 h
    since = floor
    if last:
        since = max(floor, datetime.fromtimestamp(float(last), timezone.utc))
    total = 0
    for _ in range(int(cfg["max_pages_per_collect"])):
        data = bitquery(cfg, TRADES_QUERY, {
            "signer": cfg["fomo_signer"], "protocol": cfg["fomo_protocol"],
            "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": int(cfg["rows_per_page"]), "minusd": str(cfg["min_trade_usd"])})
        rows = data["Solana"]["DEXTrades"]
        parsed = [p for p in (parse_trade(r) for r in rows) if p]
        con.executemany("INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?,?)", parsed)
        con.commit()
        total += len(parsed)
        if not rows:
            break
        newest = max(datetime.fromisoformat(r["Block"]["Time"].replace("Z", "+00:00")) for r in rows)
        meta_set(con, "last_ts", newest.timestamp())
        if len(rows) < int(cfg["rows_per_page"]) or newest <= since:
            break
        since = newest
    # on ne garde pas plus de 30 jours
    con.execute("DELETE FROM trades WHERE ts < ?", (time.time() - 30 * 86400,))
    con.commit()
    meta_set(con, "last_collect_run", time.time())
    if verbose:
        n = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        print(f"Collecte OK : {total} trades ajoutés, {n} en base.")
    return total


# --------------------------------------------------------------------------
# Analyse
# --------------------------------------------------------------------------

def wallet_stats(cfg, con, now):
    """Note chaque wallet à partir des positions fermées (achat puis vente)."""
    since = now - cfg["history_days"] * 86400
    rows = con.execute(
        "SELECT wallet, side, mint, token_amount, usd, ts FROM trades "
        "WHERE ts >= ? AND ts < ? ORDER BY ts", (since, now)).fetchall()
    db_start = con.execute("SELECT MIN(ts) FROM trades").fetchone()[0] or now
    first_seen = dict(con.execute("SELECT mint, MIN(ts) FROM trades WHERE ts < ? GROUP BY mint",
                                  (now,)).fetchall())
    by_wallet = {}
    for w, side, mint, amt, usd, ts in rows:
        by_wallet.setdefault(w, []).append((side, mint, amt, usd, ts))

    stats = {}
    for w, trades in by_wallet.items():
        pos = {}  # mint -> dict
        free_sells = 0
        sells = 0
        first, lastt = trades[0][4], trades[-1][4]
        for side, mint, amt, usd, ts in trades:
            p = pos.setdefault(mint, {"bought": 0.0, "cost": 0.0, "sold": 0.0,
                                      "proceeds": 0.0, "first_buy": None, "last_sell": None})
            if side == "buy":
                p["bought"] += amt
                p["cost"] += usd
                if p["first_buy"] is None:
                    p["first_buy"] = ts
            else:
                sells += 1
                if p["bought"] <= 0:
                    # Vend un token qu'on ne l'a jamais vu acheter. On ne compte ça comme
                    # suspect que si le token est apparu après le début de notre historique
                    # (sinon il a pu l'acheter avant qu'on commence à collecter).
                    if first_seen.get(mint, now) > db_start + 3600:
                        free_sells += 1
                    else:
                        sells -= 1
                    continue
                p["sold"] += amt
                p["proceeds"] += usd
                p["last_sell"] = ts
        closed = []
        for mint, p in pos.items():
            if p["bought"] > 0 and p["sold"] >= 0.8 * p["bought"]:
                frac = min(1.0, p["sold"] / p["bought"])
                cost = p["cost"] * frac
                pnl = p["proceeds"] - cost
                hold = ((p["last_sell"] or p["first_buy"]) - p["first_buy"]) / 60
                closed.append((mint, pnl, cost, hold))
        days = max(1.0, (lastt - first) / 86400)
        st = {"trades": len(trades), "trades_per_day": len(trades) / days,
              "closed": len(closed), "free_sell_ratio": free_sells / sells if sells else 0.0}
        if closed:
            wins = [c for c in closed if c[1] > 0]
            gains = sum(c[1] for c in wins)
            losses = -sum(c[1] for c in closed if c[1] <= 0)
            st["win_rate"] = len(wins) / len(closed)
            st["profit_factor"] = (gains / losses) if losses > 0 else (10.0 if gains > 0 else 0.0)
            st["pnl"] = sum(c[1] for c in closed)
            st["median_hold_min"] = statistics.median(c[3] for c in closed)
            st["top_share"] = (max(c[1] for c in wins) / gains) if gains > 0 else 1.0
        stats[w] = st
    return stats


def qualify(cfg, st):
    """Renvoie (ok, raison_du_rejet)."""
    if st["trades_per_day"] > cfg["max_trades_per_day"]:
        return False, "bot (trop de trades)"
    if st["free_sell_ratio"] > cfg["max_free_sell_ratio"]:
        return False, "initié (vend des tokens jamais achetés)"
    if st["closed"] < cfg["min_closed_positions"]:
        return False, "pas assez d'historique"
    if st["median_hold_min"] < cfg["min_median_hold_minutes"]:
        return False, "sniper/bot (revend en quelques secondes)"
    if st["top_share"] > cfg["max_top_token_profit_share"] and st["closed"] < 15:
        return False, "un seul gros coup de chance"
    if st["win_rate"] < cfg["min_win_rate"]:
        return False, "trop de pertes"
    if st["profit_factor"] < cfg["min_profit_factor"]:
        return False, "gains trop faibles vs pertes"
    if st["pnl"] <= 0:
        return False, "perdant"
    return True, ""


def wallet_score(st):
    pf = min(st["profit_factor"], 5.0)
    return round(pf * st["win_rate"] * math.sqrt(st["closed"]), 2)


def last_price(con, mint, before=None):
    if before is None:
        r = con.execute("SELECT price, ts FROM trades WHERE mint=? AND price>0 "
                        "ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
    else:
        r = con.execute("SELECT price, ts FROM trades WHERE mint=? AND price>0 AND ts<? "
                        "ORDER BY ts DESC LIMIT 1", (mint, before)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def price_after(con, mint, t, window=2 * 3600):
    """Prix médian des trades juste après l'instant t (pour le backtest)."""
    rows = con.execute("SELECT price FROM trades WHERE mint=? AND price>0 AND ts>=? AND ts<? "
                       "ORDER BY ts LIMIT 15", (mint, t, t + window)).fetchall()
    if not rows:
        return None
    ps = sorted(r[0] for r in rows)
    return ps[len(ps) // 2]


# --------------------------------------------------------------------------
# Vrai prix + sécurité du token (sources gratuites, sans clé)
# --------------------------------------------------------------------------

def ext_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS market(
        mint TEXT PRIMARY KEY, price REAL, liq REAL, mcap REAL, created REAL,
        vol24 REAL, ch1 REAL, ch24 REAL, url TEXT, fetched REAL)""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(market)")}
    if "sym" not in cols:
        con.execute("ALTER TABLE market ADD COLUMN sym TEXT")
    if "pair" not in cols:
        con.execute("ALTER TABLE market ADD COLUMN pair TEXT")
    con.execute("CREATE TABLE IF NOT EXISTS ohlcv(mint TEXT PRIMARY KEY, data TEXT, fetched REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS rank_snap(ts REAL, wallet TEXT, rank INTEGER, score REAL)")
    con.execute("""CREATE TABLE IF NOT EXISTS safety(
        mint TEXT PRIMARY KEY, score INTEGER, danger INTEGER, warn INTEGER,
        lp_locked REAL, risks TEXT, fetched REAL)""")


def http_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "FomoRecap/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_market(cfg, con, mints):
    """Prix réel, liquidité, capitalisation via DexScreener (30 tokens par appel)."""
    ext_tables(con)
    ttl = cfg["market_cache_minutes"] * 60
    now = time.time()
    todo = [m for m in dict.fromkeys(mints) if m and not con.execute(
        "SELECT 1 FROM market WHERE mint=? AND fetched>?", (m, now - ttl)).fetchone()]
    for i in range(0, len(todo), 30):
        chunk = todo[i:i + 30]
        try:
            pairs = http_json("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk))
        except Exception:
            continue
        best = {}
        for p in pairs if isinstance(pairs, list) else []:
            m = (p.get("baseToken") or {}).get("address")
            liq = ((p.get("liquidity") or {}).get("usd")) or 0
            if m in chunk and (m not in best or liq > best[m][0]):
                best[m] = (liq, p)
        for m in chunk:
            if m not in best:
                con.execute("INSERT OR REPLACE INTO market(mint, price, liq, mcap, created, vol24, ch1, ch24, url, fetched, sym) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (m, None, 0, None, None, None, None, None, None, now, None))
                continue
            liq, p = best[m]
            created = p.get("pairCreatedAt")
            con.execute("INSERT OR REPLACE INTO market(mint, price, liq, mcap, created, vol24, ch1, ch24, url, fetched, sym, pair) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                m, f(p.get("priceUsd")) or None, liq, p.get("marketCap") or p.get("fdv"),
                created / 1000 if created else None, (p.get("volume") or {}).get("h24"),
                (p.get("priceChange") or {}).get("h1"), (p.get("priceChange") or {}).get("h24"),
                p.get("url"), now, (p.get("baseToken") or {}).get("symbol"), p.get("pairAddress")))
        con.commit()


def fetch_safety(cfg, con, mints, max_calls=8):
    """Résumé de risques RugCheck (autorité de mint, gros détenteurs, liquidité non bloquée...)."""
    ext_tables(con)
    ttl = cfg["safety_cache_hours"] * 3600
    now = time.time()
    calls = 0
    for m in dict.fromkeys(mints):
        if not m or calls >= max_calls:
            continue
        if con.execute("SELECT 1 FROM safety WHERE mint=? AND fetched>?", (m, now - ttl)).fetchone():
            continue
        calls += 1
        try:
            d = http_json(f"https://api.rugcheck.xyz/v1/tokens/{m}/report/summary")
        except Exception:
            continue
        risks = [{"name": r.get("name"), "level": r.get("level"), "description": r.get("description"),
                  "value": r.get("value")} for r in (d.get("risks") or [])]
        danger = sum(1 for r in risks if (r["level"] or "").lower() == "danger")
        warn = sum(1 for r in risks if (r["level"] or "").lower() == "warn")
        con.execute("INSERT OR REPLACE INTO safety VALUES(?,?,?,?,?,?,?)",
                    (m, d.get("score_normalised", d.get("score")), danger, warn,
                     d.get("lpLockedPct"), json.dumps(risks, ensure_ascii=False), now))
        con.commit()


def market_info(con, mint):
    ext_tables(con)
    r = con.execute("SELECT price, liq, mcap, created, vol24, ch1, ch24, url, fetched, pair FROM market WHERE mint=?",
                    (mint,)).fetchone()
    if not r:
        return None
    keys = ("price", "liq", "mcap", "created", "vol24", "ch1", "ch24", "url", "fetched", "pair")
    return dict(zip(keys, r))


def safety_info(con, mint):
    ext_tables(con)
    r = con.execute("SELECT score, danger, warn, lp_locked, risks, fetched FROM safety WHERE mint=?",
                    (mint,)).fetchone()
    if not r:
        return None
    return {"score": r[0], "danger": r[1], "warn": r[2], "lp_locked": r[3],
            "risks": json.loads(r[4] or "[]"), "fetched": r[5]}


def best_price(con, mint, now=None):
    """Prix du marché (DexScreener) s'il est récent, sinon dernier prix vu sur Fomo.
    Pour rejouer le passé (backtest), seul le prix Fomo d'avant 'now' est utilisé."""
    live = now is None or abs(time.time() - now) < 300
    if live:
        mk = market_info(con, mint)
        if mk and mk["price"] and time.time() - mk["fetched"] < 1800:
            return mk["price"], mk["fetched"]
        return last_price(con, mint)
    return last_price(con, mint, before=now)


_GECKO_LAST = [0.0]


def gecko_json(url):
    """GeckoTerminal gratuit : 30 appels/min max -> on espace les appels de 2,2 s."""
    wait = 2.2 - (time.time() - _GECKO_LAST[0])
    if wait > 0:
        time.sleep(wait)
    _GECKO_LAST[0] = time.time()
    return http_json(url, timeout=15)


def pool_of(cfg, con, mint):
    mk = market_info(con, mint)
    if not mk or not mk.get("pair"):
        try:
            con.execute("DELETE FROM market WHERE mint=? AND pair IS NULL", (mint,))
            fetch_market(cfg, con, [mint])
        except Exception:
            pass
        mk = market_info(con, mint)
    return mk.get("pair") if mk else None


def candles_hist(cfg, con, mint, need_until):
    """Bougies 15 min sur ~10 jours (pour le backtest), en cache ; rafraîchies si trop courtes."""
    ext_tables(con)
    con.execute("CREATE TABLE IF NOT EXISTS ohlcv_hist(mint TEXT PRIMARY KEY, data TEXT, fetched REAL)")
    r = con.execute("SELECT data, fetched FROM ohlcv_hist WHERE mint=?", (mint,)).fetchone()
    if r:
        data = json.loads(r[0])
        last = data[-1][0] if data else 0
        if last + 900 >= need_until or time.time() - r[1] < 1800:
            return data
    pool = pool_of(cfg, con, mint)
    if not pool:
        return json.loads(r[0]) if r else []
    try:
        d = gecko_json(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}"
                       f"/ohlcv/minute?aggregate=15&limit=1000&currency=usd")
        rows = (((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        data = sorted([[c[0], c[1], c[2], c[3], c[4], c[5]] for c in rows if len(c) >= 5])
    except Exception:
        data = json.loads(r[0]) if r else []
    con.execute("INSERT OR REPLACE INTO ohlcv_hist VALUES(?,?,?)", (mint, json.dumps(data), time.time()))
    con.commit()
    return data


def price_at(candles, t, tol=3600):
    """Prix à l'instant t d'après les bougies : clôture de la bougie qui contient t, ou la plus proche (< 1 h)."""
    best = None
    for c in candles:
        if c[0] <= t < c[0] + 900:
            return c[4]
        d = min(abs(c[0] - t), abs(c[0] + 900 - t))
        if d <= tol and (best is None or d < best[0]):
            best = (d, c[4] if c[0] < t else c[1])
    return best[1] if best else None


def fetch_ohlcv(con, mint, ttl=300):
    """Bougies de 15 min sur ~24 h via GeckoTerminal (gratuit, sans clé, 30 appels/min)."""
    ext_tables(con)
    r = con.execute("SELECT data, fetched FROM ohlcv WHERE mint=?", (mint,)).fetchone()
    if r and time.time() - r[1] < ttl:
        return json.loads(r[0])
    mk = market_info(con, mint)
    if not mk or not mk.get("pair"):
        return json.loads(r[0]) if r else []
    try:
        d = http_json(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{mk['pair']}"
                      f"/ohlcv/minute?aggregate=15&limit=96&currency=usd")
        rows = (((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        candles = sorted([[c[0], c[1], c[2], c[3], c[4], c[5]] for c in rows if len(c) >= 5])
        con.execute("INSERT OR REPLACE INTO ohlcv VALUES(?,?,?)", (mint, json.dumps(candles), time.time()))
        con.commit()
        return candles
    except Exception:
        return json.loads(r[0]) if r else []


def trade_plan(cfg, price, entry):
    """Repères simples tirés des réglages (pas une prédiction) : zone d'achat, stop, objectifs."""
    if not price:
        return None
    ref = entry or price
    zone_low, zone_high = ref * 0.95, ref * 1.10
    return {"zone_low": zone_low, "zone_high": zone_high, "in_zone": zone_low <= price <= zone_high,
            "above_zone": price > zone_high,
            "stop": price * (1 + cfg["stop_loss_pct"] / 100),
            "target1": price * (1 + cfg["take_profit_pct"] / 200),
            "target2": price * (1 + cfg["take_profit_pct"] / 100),
            "stop_pct": cfg["stop_loss_pct"], "t1_pct": cfg["take_profit_pct"] / 2, "t2_pct": cfg["take_profit_pct"]}


def snapshot_ranks(con, good, every=3 * 3600):
    """Photo du classement (top 60) toutes les 3 h, pour voir qui monte, qui descend, qui arrive."""
    ext_tables(con)
    last = con.execute("SELECT MAX(ts) FROM rank_snap").fetchone()[0]
    if last and time.time() - last < every:
        return
    now = time.time()
    for i, (w, sc) in enumerate(sorted(good.items(), key=lambda x: -x[1])[:60], 1):
        con.execute("INSERT INTO rank_snap VALUES(?,?,?,?)", (now, w, i, sc))
    con.execute("DELETE FROM rank_snap WHERE ts < ?", (now - 10 * 86400,))
    con.commit()


def rank_changes(con, good):
    """Compare le classement actuel à la photo d'il y a ~24 h (ou la plus ancienne dispo, min 3 h)."""
    ext_tables(con)
    now = time.time()
    ref = con.execute("SELECT ts FROM rank_snap WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (now - 24 * 3600,)).fetchone()
    if not ref:
        ref = con.execute("SELECT MIN(ts) FROM rank_snap WHERE ts <= ?", (now - 3 * 3600,)).fetchone()
    if not ref or not ref[0]:
        return {"since": None, "prev": {}, "gone": []}
    prev = {w: r for w, r in con.execute("SELECT wallet, rank FROM rank_snap WHERE ts=?", (ref[0],))}
    current = [w for w, _ in sorted(good.items(), key=lambda x: -x[1])[:60]]
    gone = [w for w, r in sorted(prev.items(), key=lambda x: x[1]) if w not in current][:10]
    return {"since": ref[0], "prev": prev, "gone": gone}


def enrich(cfg, con, mints):
    """Met à jour prix réel + sécurité pour une liste de tokens (avec cache)."""
    mints = [m for m in dict.fromkeys(mints) if m]
    try:
        fetch_market(cfg, con, mints)
    except Exception:  # y compris base occupée : les infos seront mises à jour plus tard
        pass
    try:
        fetch_safety(cfg, con, mints)
    except Exception:
        pass


def build_recap(cfg, con, now=None, limit=None, details=False):
    """Classe les candidats. now permet de rejouer le passé (backtest) sans voir le futur."""
    now = now or time.time()
    stats = wallet_stats(cfg, con, now)
    good, rejected = {}, {}
    for w, st in stats.items():
        if st["closed"] == 0 and st["trades_per_day"] <= cfg["max_trades_per_day"]:
            continue
        ok, why = qualify(cfg, st)
        if ok:
            good[w] = wallet_score(st)
        else:
            rejected[why] = rejected.get(why, 0) + 1

    # Achats récents des bons traders, encore en position
    since = now - cfg["fresh_buy_hours"] * 3600
    cands = {}
    positions = {}
    for w, score in good.items():
        rows = con.execute(
            "SELECT mint, symbol, side, token_amount, usd, price, ts FROM trades "
            "WHERE wallet=? AND ts>=? AND ts<? ORDER BY ts", (w, since, now)).fetchall()
        held = {}
        for mint, sym, side, amt, usd, price, ts in rows:
            h = held.setdefault(mint, {"sym": sym, "bought": 0.0, "sold": 0.0, "cost": 0.0, "ts": ts})
            if side == "buy":
                h["bought"] += amt
                h["cost"] += usd
            else:
                h["sold"] += amt
        for mint, h in held.items():
            if h["bought"] <= 0 or h["cost"] <= 0 or h["sold"] >= 0.5 * h["bought"]:
                continue  # rien acheté, ou déjà revendu en grande partie
            positions.setdefault(w, []).append({"mint": mint, "sym": h["sym"], "ts": h["ts"],
                                                "entry": h["cost"] / h["bought"], "usd": h["cost"]})
            c = cands.setdefault(mint, {"sym": h["sym"], "wallets": [], "cost": 0.0,
                                        "amt": 0.0, "score": 0.0, "first": h["ts"]})
            c["wallets"].append((w, score))
            c["cost"] += h["cost"]
            c["amt"] += h["bought"]
            c["score"] += score
            c["first"] = min(c["first"], h["ts"])

    ranked = []
    for mint, c in cands.items():
        entry = c["cost"] / c["amt"] if c["amt"] else None
        price, pts = best_price(con, mint, now)
        alerts = []
        runup = (price / entry) if (price and entry) else None
        if runup and runup > cfg["max_runup"]:
            continue  # trop tard, le prix a déjà décollé
        first_seen = con.execute("SELECT MIN(ts) FROM trades WHERE mint=? AND ts<?", (mint, now)).fetchone()[0]
        if first_seen and now - first_seen < 24 * 3600:
            alerts.append("token très récent")
        if len(c["wallets"]) == 1:
            alerts.append("un seul bon trader dessus")
        if runup and runup > 1.2:
            alerts.append(f"déjà +{(runup - 1) * 100:.0f}% depuis leur achat")
        if pts and now - pts > 6 * 3600:
            alerts.append("prix pas mis à jour depuis 6 h")
        final = c["score"] * (1 + 0.3 * (len(c["wallets"]) - 1)) / (runup if runup and runup > 1 else 1)
        live = abs(time.time() - now) < 300
        mk = market_info(con, mint) if live else None
        sf = safety_info(con, mint) if live else None
        ranked.append({"liq": mk["liq"] if mk else None, "mcap": mk["mcap"] if mk else None,
                       "pair_created": mk["created"] if mk else None,
                       "safety": sf, "market_ok": mk is not None,
                       "mint": mint, "sym": c["sym"], "n": len(c["wallets"]),
                       "entry": entry, "price": price, "alerts": alerts,
                       "score": round(final, 2), "price_ts": pts, "first_buy": c["first"],
                       "wallets": [w for w, _ in c["wallets"]]})
    ranked.sort(key=lambda x: x["score"], reverse=True)
    top = ranked[: int(limit or cfg["max_candidates"])]
    if details:
        return stats, good, rejected, top, positions
    return stats, good, rejected, top


def tracking_lines(cfg, con):
    """Suivi des propositions précédentes (sans argent réel)."""
    rows = con.execute("SELECT day, rank, mint, symbol, price FROM picks "
                       "WHERE rank=1 ORDER BY day DESC LIMIT 30").fetchall()
    res = []
    for day, rank, mint, sym, p0 in rows:
        p1, _ = best_price(con, mint)
        if p0 and p1:
            res.append((day, sym, (p1 / p0 - 1) * 100 - cfg["assumed_costs_pct"]))
    if not res:
        return ["Suivi : pas encore de propositions passées."]
    avg = sum(r[2] for r in res) / len(res)
    wins = sum(1 for r in res if r[2] > 0)
    lines = [f"Suivi des n°1 ({len(res)} jours, frais estimés déduits) : "
             f"moyenne {avg:+.1f}%, {wins}/{len(res)} gagnants."]
    for day, sym, perf in res[:3]:
        lines.append(f"  {day} {sym} : {perf:+.1f}%")
    return lines


def fmt_price(p):
    if p is None:
        return "?"
    if p >= 1:
        return f"{p:.3f} $"
    return f"{p:.10f}".rstrip("0") + " $"


def recap_text(cfg, con, save=True):
    try:
        enriched_recap(cfg, con)  # applique l'assouplissement automatique si trop peu de traders
    except Exception:
        pass
    stats, good, rejected, top = build_recap(cfg, con)
    n_trades = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    oldest = con.execute("SELECT MIN(ts) FROM trades").fetchone()[0]
    hist_days = (time.time() - oldest) / 86400 if oldest else 0
    day = datetime.now().strftime("%Y-%m-%d")

    L = [f"Récap Fomo du {datetime.now().strftime('%d/%m %H:%M')}"]
    L.append(f"{len(stats)} traders analysés, {len(good)} retenus. "
             f"Historique : {hist_days:.1f} jours, {n_trades} trades.")
    if rejected:
        rej = ", ".join(f"{v} {k}" for k, v in sorted(rejected.items(), key=lambda x: -x[1]))
        L.append(f"Écartés : {rej}.")
    if hist_days < 3:
        L.append("Attention : moins de 3 jours d'historique, le tri n'est pas encore fiable.")
    L.append("")
    if not top:
        L.append("Aucun candidat ce matin. Ne rien acheter est aussi une bonne réponse.")
    for i, c in enumerate(top, 1):
        L.append(f"{i}. {c['sym']}  (score {c['score']})")
        L.append(f"   Acheté par {c['n']} bon(s) trader(s), prix moyen {fmt_price(c['entry'])}, "
                 f"dernier prix {fmt_price(c['price'])}")
        if c["alerts"]:
            L.append("   Alertes : " + ", ".join(c["alerts"]))
        L.append(f"   Adresse : {c['mint']}")
    L.append("")
    L.extend(tracking_lines(cfg, con))
    L.append("")
    L.append("Ce n'est pas une garantie. Petites mises seulement, et c'est toi qui décides.")

    if save:
        for i, c in enumerate(top, 1):
            con.execute("INSERT OR REPLACE INTO picks VALUES(?,?,?,?,?,?)",
                        (day, i, c["mint"], c["sym"], c["price"], c["score"]))
        con.commit()
    return "\n".join(L)


# --------------------------------------------------------------------------
# Signal du moment (feu vert / orange / rouge)
# --------------------------------------------------------------------------

def tracking_stats(cfg, con):
    vals = []
    for mint, p0 in con.execute("SELECT mint, price FROM picks WHERE rank=1 ORDER BY day DESC LIMIT 30"):
        p1, _ = best_price(con, mint)
        if p0 and p1:
            vals.append((p1 / p0 - 1) * 100 - cfg["assumed_costs_pct"])
    return len(vals), (sum(vals) / len(vals) if vals else None)


def signal_for(cfg, cand, hist_days, track_n, track_avg, now):
    change = (cand["price"] / cand["entry"] - 1) * 100 if cand.get("price") and cand.get("entry") else None
    fresh_min = (now - cand["price_ts"]) / 60 if cand.get("price_ts") else None
    checks = [
        {"key": "history", "label": f"Au moins {cfg['signal_min_history_days']} jours d'historique collecté",
         "ok": hist_days >= cfg["signal_min_history_days"], "blocking": True,
         "detail": f"{hist_days:.1f} jour(s)".replace(".", ",")},
        {"key": "fresh", "label": "Prix vu il y a moins d'1 heure",
         "ok": fresh_min is not None and fresh_min <= 60, "blocking": True,
         "detail": "–" if fresh_min is None else f"il y a {fresh_min:.0f} min"},
        {"key": "falling", "label": "Pas en chute (plus de -15 % depuis leur achat)",
         "ok": change is None or change >= -15, "blocking": True,
         "detail": "–" if change is None else f"{change:+.0f} %"},
        {"key": "liquidity", "label": f"Liquidité d'au moins {cfg['min_liquidity_usd']:,} $".replace(",", " "),
         "ok": bool(cand.get("liq")) and cand["liq"] >= cfg["min_liquidity_usd"], "blocking": bool(cand.get("market_ok")),
         "detail": "inconnue" if not cand.get("market_ok") else f"{(cand.get('liq') or 0):,.0f} $".replace(",", " ")},
        {"key": "safety", "label": "Aucun risque grave détecté (RugCheck)",
         "ok": bool(cand.get("safety")) and cand["safety"]["danger"] == 0,
         "blocking": bool(cand.get("safety")) and cand["safety"]["danger"] > 0,
         "detail": "pas encore vérifié" if not cand.get("safety") else
                   (f"{cand['safety']['danger']} risque(s) grave(s)" if cand["safety"]["danger"]
                    else f"{cand['safety']['warn']} point(s) d'attention")},
        {"key": "traders", "label": "Au moins 2 bons traders dessus",
         "ok": cand["n"] >= 2, "blocking": False, "detail": str(cand["n"])},
        {"key": "late", "label": "Pas déjà envolé (moins de +20 % depuis leur achat)",
         "ok": change is None or change <= 20, "blocking": False,
         "detail": "–" if change is None else f"{change:+.0f} %"},
        {"key": "young", "label": "Token pas créé il y a moins de 24 h",
         "ok": "token très récent" not in cand["alerts"], "blocking": False, "detail": ""},
        {"key": "track", "label": f"Suivi positif sur au moins {cfg['signal_min_track_days']} jours",
         "ok": track_n >= cfg["signal_min_track_days"] and (track_avg or 0) > 0, "blocking": False,
         "detail": "pas encore de suivi" if not track_n else f"{track_n} j, moyenne {track_avg:+.1f} %".replace(".", ",")},
    ]
    if any(c["blocking"] and not c["ok"] for c in checks):
        level = "red"
    elif all(c["ok"] for c in checks):
        level = "green"
    else:
        level = "orange"
    return level, checks, change


VERDICT_TEXT = {
    "green": ("Signal fort", "Tous les critères sont validés. Si tu y vas : petite mise, et décide d'avance quand tu sors."),
    "orange": ("À surveiller", "Des bons traders sont dessus, mais tous les critères ne sont pas réunis. Observe plutôt que d'acheter."),
    "red": ("Rien à prendre", "Un critère indispensable n'est pas rempli. Ne rien acheter est la bonne réponse ici."),
}


def price_series(con, mint, since, max_points=160):
    rows = con.execute("SELECT ts, price FROM trades WHERE mint=? AND ts>=? AND price>0 ORDER BY ts",
                       (mint, since)).fetchall()
    if len(rows) <= max_points:
        return [[t, p] for t, p in rows]
    step = len(rows) / max_points
    out = []
    for i in range(max_points):
        chunk = rows[int(i * step):int((i + 1) * step)] or rows[-1:]
        ps = sorted(p for _, p in chunk)
        out.append([chunk[len(chunk) // 2][0], ps[len(ps) // 2]])  # médiane : ignore les prix aberrants
    return out


def build_signal(cfg, con, top, hist_days, now):
    track_n, track_avg = tracking_stats(cfg, con)
    if not top:
        return {"level": "red", "title": "Rien à prendre",
                "text": "Aucun bon trader n'a acheté récemment un token qu'il garde encore.",
                "candidate": None, "checks": [], "track_n": track_n, "track_avg": track_avg}
    scored = []
    for c in top:
        level, checks, change = signal_for(cfg, c, hist_days, track_n, track_avg, now)
        rank = {"green": 0, "orange": 1, "red": 2}[level]
        scored.append((rank, -c["score"], c, level, checks, change))
    scored.sort(key=lambda x: (x[0], x[1]))
    _, _, c, level, checks, change = scored[0]
    since = min(now - 12 * 3600, (c.get("first_buy") or now) - 3600)
    buys = []
    if c.get("wallets"):
        q = ",".join("?" * len(c["wallets"]))
        buys = con.execute(f"SELECT ts, price, usd FROM trades WHERE mint=? AND side='buy' "
                           f"AND wallet IN ({q}) AND ts>=? ORDER BY ts",
                           [c["mint"], *c["wallets"], since]).fetchall()
    title, text = VERDICT_TEXT[level]
    cand = {k: c[k] for k in ("mint", "sym", "n", "entry", "price", "alerts", "score", "price_ts")}
    cand["change"] = change
    return {"level": level, "title": title, "text": text, "candidate": cand, "checks": checks,
            "series": price_series(con, c["mint"], since),
            "smart_buys": [[t, p, u] for t, p, u in buys],
            "track_n": track_n, "track_avg": track_avg}


RELAXED = {"min_closed_positions": 4, "min_profit_factor": 1.3, "min_win_rate": 0.45}


def enriched_recap(cfg, con):
    """Classement + mise à jour du vrai prix et de la sécurité des tokens utiles, puis reclassement.
    Si trop peu de traders passent le tri, les critères sont assouplis d'un cran (cfg['_relaxed'])."""
    stats, good, rejected, ranked, positions = build_recap(cfg, con, limit=200, details=True)
    if len(good) < cfg.get("min_retained", 20) and not cfg.get("_relaxed"):
        cfg.update(RELAXED)
        cfg["_relaxed"] = True
        stats, good, rejected, ranked, positions = build_recap(cfg, con, limit=200, details=True)
    mints = [c["mint"] for c in ranked[:15]]
    for plist in positions.values():
        mints += [p["mint"] for p in plist]
    try:
        my_table(con)
        mints += [r[0] for r in con.execute("SELECT mint FROM my_trades WHERE exit IS NULL AND mint IS NOT NULL")]
    except Exception:
        pass
    enrich(cfg, con, mints[:60])
    return build_recap(cfg, con, limit=200, details=True)


def check_alerts(cfg, con, green=True, exits=True):
    """Alertes Telegram : 1re apparition d'un feu vert (par token et par jour) et signaux de sortie
    sur tes trades en cours (une fois par trade et par type)."""
    if not cfg.get("telegram_chat_id"):
        return None
    now = time.time()
    stats, good, rejected, ranked, positions = enriched_recap(cfg, con)
    top = ranked[: int(cfg["max_candidates"])]
    oldest = con.execute("SELECT MIN(ts) FROM trades").fetchone()[0]
    hist = (now - oldest) / 86400 if oldest else 0
    sent = None
    if green and cfg.get("alert_on_green", True):
        sig = build_signal(cfg, con, top, hist, now)
        c = sig.get("candidate")
        if sig["level"] == "green" and c:
            key = "alert_" + datetime.now().strftime("%Y-%m-%d") + "_" + c["mint"]
            if not meta_get(con, key):
                bt = backtest_data(cfg, con)
                test = "" if bt["measured"] >= bt["target"] else "PHASE DE TEST (backtest pas encore fiable) : ne mise pas, observe.\n"
                send(cfg, test + f"FEU VERT Fomo : {c['sym']}\n"
                          f"{c['n']} bons traders dessus, {c['change']:+.0f} % depuis leur achat.\n"
                          f"Adresse : {c['mint']}\n"
                          f"Ouvre l'appli Fomo Recap pour voir le graphique. Petite mise seulement, c'est toi qui décides.")
                meta_set(con, key, now)
                sent = c["sym"]
    if exits and cfg.get("alert_on_exit", True) and not cfg.get("_cloud"):
        for t in my_trades(cfg, con, good)["trades"]:
            if t["advice"]:
                key = f"exit_{t['id']}_{t['advice']}"
                if not meta_get(con, key):
                    send(cfg, f"SORTIE ? {t['sym']} : {t['advice_msg']}\n"
                              f"Ouvre Fomo Recap > Mes trades. C'est toi qui décides.")
                    meta_set(con, key, now)
    return sent


# --------------------------------------------------------------------------
# Classement des traders, détail d'un wallet
# --------------------------------------------------------------------------

LEVEL_RANK = {"green": 0, "orange": 1, "red": 2, None: 3}


def trader_row(w, score, st):
    return {"wallet": w, "score": score, "win_rate": st.get("win_rate"),
            "profit_factor": st.get("profit_factor"), "pnl": st.get("pnl"),
            "closed": st.get("closed"), "median_hold_min": st.get("median_hold_min"),
            "trades_per_day": st.get("trades_per_day")}


def trader_board(cfg, con, stats, good, ranked, positions, hist_days, track_n, track_avg, now):
    by_mint = {c["mint"]: c for c in ranked}
    board = []
    for w, score in good.items():
        row = trader_row(w, score, stats[w])
        best = None
        for p in positions.get(w, []):
            c = by_mint.get(p["mint"])
            price, pts = best_price(con, p["mint"])
            if c:
                level, checks, change = signal_for(cfg, c, hist_days, track_n, track_avg, now)
                passed = sum(1 for k in checks if k["ok"])
                total = len(checks)
                n = c["n"]
            else:  # exclu du classement : prix déjà envolé
                level, passed, total, n = "red", 0, 7, 1
            mine = (price / p["entry"] - 1) * 100 if price and p["entry"] else None
            pos = {"mint": p["mint"], "sym": p["sym"], "entry": p["entry"], "usd": p["usd"],
                   "ts": p["ts"], "price": price, "change": mine, "level": level,
                   "passed": passed, "total": total, "n": n}
            if best is None or (LEVEL_RANK[level], -p["ts"]) < (LEVEL_RANK[best["level"]], -best["ts"]):
                best = pos
        row["position"] = best
        row["open_count"] = len(positions.get(w, []))
        board.append(row)
    board.sort(key=lambda r: (LEVEL_RANK[r["position"]["level"] if r["position"] else None],
                              -(r["position"]["passed"] if r["position"] else 0), -r["score"]))
    return board


def wallet_detail(cfg, con, wallet):
    now = time.time()
    rows = con.execute("SELECT ts, side, mint, symbol, token_amount, usd, price FROM trades "
                       "WHERE wallet=? ORDER BY ts", (wallet,)).fetchall()
    pos = {}
    for ts, side, mint, sym, amt, usd, price in rows:
        p = pos.setdefault(mint, {"mint": mint, "sym": sym, "bought": 0.0, "cost": 0.0, "sold": 0.0,
                                  "proceeds": 0.0, "first": ts, "last": ts})
        p["last"] = ts
        if side == "buy":
            p["bought"] += amt
            p["cost"] += usd
        else:
            p["sold"] += amt
            p["proceeds"] += usd
    out = []
    for p in pos.values():
        price, _ = best_price(con, p["mint"])
        if p["bought"] <= 0:
            status, pnl = "vendu (achat non vu)", None
        elif p["sold"] >= 0.8 * p["bought"]:
            frac = min(1.0, p["sold"] / p["bought"])
            status, pnl = "fermée", p["proceeds"] - p["cost"] * frac
        else:
            held = p["bought"] - p["sold"]
            status = "en cours"
            pnl = p["proceeds"] + (held * price if price else 0) - p["cost"]
        out.append({"mint": p["mint"], "sym": p["sym"], "status": status, "cost": p["cost"],
                    "proceeds": p["proceeds"], "pnl": pnl, "first": p["first"], "last": p["last"],
                    "price": price, "entry": p["cost"] / p["bought"] if p["bought"] else None})
    out.sort(key=lambda x: -x["last"])
    stats = wallet_stats(cfg, con, now).get(wallet, {})
    ok, why = (qualify(cfg, stats) if stats.get("closed") else (False, "pas assez d'historique"))
    recent = [{"ts": ts, "side": side, "mint": mint, "sym": sym, "usd": usd, "price": price}
              for ts, side, mint, sym, amt, usd, price in rows[-40:][::-1]]
    return {"wallet": wallet, "stats": trader_row(wallet, wallet_score(stats) if stats.get("closed") else 0, stats),
            "retained": ok, "reason": why, "positions": out[:40], "recent": recent}


# --------------------------------------------------------------------------
# Backtest : rejoue les règles toutes les 6 h sans regarder le futur
# --------------------------------------------------------------------------

BT_STEP = 6 * 3600


def bt_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS backtest(
        slot REAL PRIMARY KEY, mint TEXT, sym TEXT, level TEXT, n INTEGER, passed INTEGER,
        entry REAL, p6 REAL, p24 REAL)""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(backtest)")}
    for col, typ in (("b_mint", "TEXT"), ("b_sym", "TEXT"), ("b_entry", "REAL"), ("b_p6", "REAL"), ("b_p24", "REAL"), ("entry_v", "INTEGER"), ("fails", "TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE backtest ADD COLUMN {col} {typ}")


def baseline_pick(con, slot):
    """Référence 'sans réfléchir' : le token le plus acheté sur Fomo dans les 6 h avant le créneau."""
    r = con.execute("SELECT mint, symbol FROM trades WHERE side='buy' AND ts>=? AND ts<? "
                    "GROUP BY mint ORDER BY SUM(usd) DESC LIMIT 1", (slot - 6 * 3600, slot)).fetchone()
    if not r:
        return None, "", None
    price, _ = last_price(con, r[0], before=slot)
    return r[0], r[1], price


def run_backtest(cfg, con, max_new=6):
    """Calcule les créneaux manquants (limité à max_new par appel). Renvoie le nb de choses faites.
    Les résultats (+6 h / +24 h) sont mesurés sur les vraies bougies de prix (GeckoTerminal)."""
    bt_table(con)
    now = time.time()
    if not meta_get(con, "bt_v2"):  # ancienne version : on remesure ce qui n'avait pas de prix
        con.execute("UPDATE backtest SET p6=NULL WHERE p6=-1")
        con.execute("UPDATE backtest SET p24=NULL WHERE p24=-1")
        con.execute("UPDATE backtest SET b_p6=NULL WHERE b_p6=-1")
        con.execute("UPDATE backtest SET b_p24=NULL WHERE b_p24=-1")
        con.commit()
        meta_set(con, "bt_v2", 1)
    db_start = con.execute("SELECT MIN(ts) FROM trades").fetchone()[0]
    if not db_start:
        return 0
    first_slot = max(db_start + 12 * 3600, now - cfg["backtest_days"] * 86400)
    slot = math.ceil(first_slot / BT_STEP) * BT_STEP
    done = 0
    while slot <= now - 1800 and done < max_new:
        row = con.execute("SELECT mint, b_sym FROM backtest WHERE slot=?", (slot,)).fetchone()
        if row is None:
            _, _, _, top = build_recap(cfg, con, now=slot, limit=20)
            hist = (slot - db_start) / 86400
            best = None
            for c in top:
                level, checks, _ = signal_for(cfg, c, hist, 0, None, slot)
                # pas de suivi, de liquidité ni d'analyse de sécurité pour le passé
                checks = [k for k in checks if k["key"] not in ("track", "liquidity", "safety")]
                if any(k["blocking"] and not k["ok"] for k in checks):
                    level = "red"
                elif all(k["ok"] for k in checks):
                    level = "green"
                else:
                    level = "orange"
                key = (LEVEL_RANK[level], -c["score"])
                if best is None or key < best[0]:
                    best = (key, c, level, sum(1 for k in checks if k["ok"]),
                            ",".join(k["key"] for k in checks if not k["ok"]))
            if best:
                _, c, level, passed, fails = best
                vals = (slot, c["mint"], c["sym"], level, c["n"], passed, c["price"], fails)
            else:
                vals = (slot, None, None, None, 0, 0, None, None)
            con.execute("INSERT OR REPLACE INTO backtest(slot, mint, sym, level, n, passed, entry, fails) "
                        "VALUES(?,?,?,?,?,?,?,?)", vals)
            con.commit()
            done += 1
            row = (vals[1], None)
        if row[1] is None:  # référence pas encore calculée
            bm, bs, _ = baseline_pick(con, slot)
            con.execute("UPDATE backtest SET b_mint=?, b_sym=? WHERE slot=?", (bm, bs, slot))
            con.commit()
        mint, entry, p6, p24, bm, be, bp6, bp24, ev = con.execute(
            "SELECT mint, entry, p6, p24, b_mint, b_entry, b_p6, b_p24, entry_v FROM backtest WHERE slot=?", (slot,)).fetchone()
        for m, e_col, col6, col24, v6, v24, e_val in ((mint, "entry", "p6", "p24", p6, p24, ev),
                                                      (bm, "b_entry", "b_p6", "b_p24", bp6, bp24, be if be and be > 0 else None)):
            if not m or (v6 is not None and v24 is not None and (e_col != "entry" or ev)):
                continue
            ready6, ready24 = slot + 6 * 3600 + 1800 <= now, slot + 24 * 3600 + 1800 <= now
            if (v6 is None and ready6) or (v24 is None and ready24) or (e_col == "entry" and not ev) or (e_col == "b_entry" and not e_val):
                candles = candles_hist(cfg, con, m, min(now, slot + 25 * 3600))
                if not candles:
                    if slot < now - 3 * 86400:  # token introuvable : on abandonne ce créneau
                        con.execute(f"UPDATE backtest SET {col6}=COALESCE({col6},-1), {col24}=COALESCE({col24},-1) WHERE slot=?", (slot,))
                        if e_col == "entry":
                            con.execute("UPDATE backtest SET entry_v=-1 WHERE slot=?", (slot,))
                        else:
                            con.execute("UPDATE backtest SET b_entry=-1 WHERE slot=?", (slot,))
                        con.commit()
                        done += 1
                    continue
                e = price_at(candles, slot)
                if e_col == "entry" and not ev:
                    con.execute("UPDATE backtest SET entry=COALESCE(?, entry), entry_v=? WHERE slot=?", (e, 1 if e else -1, slot))
                    done += 1
                elif e_col == "b_entry" and not e_val:
                    con.execute("UPDATE backtest SET b_entry=? WHERE slot=?", (e or -1, slot))
                    done += 1
                if v6 is None and ready6:
                    con.execute(f"UPDATE backtest SET {col6}=? WHERE slot=?", (price_at(candles, slot + 6 * 3600) or -1, slot))
                    done += 1
                if v24 is None and ready24:
                    con.execute(f"UPDATE backtest SET {col24}=? WHERE slot=?", (price_at(candles, slot + 24 * 3600) or -1, slot))
                    done += 1
                con.commit()
        slot += BT_STEP
    return done


CHECK_LABELS = {
    "history": "historique trop court", "fresh": "prix pas assez récent", "falling": "token en chute",
    "liquidity": "liquidité trop faible", "safety": "risque grave (RugCheck)", "traders": "un seul bon trader dessus",
    "late": "prix déjà envolé", "young": "token trop récent", "track": "suivi pas encore positif"}


def verdicts(cfg, bt, retained, relaxed):
    """Les 5 points à surveiller, évalués automatiquement."""
    reliable = bt["measured"] >= bt["target"]
    k = "24" if bt["all24"]["n"] else "6"
    ours, base = bt["all" + k], bt["base" + k]
    edge = None
    if ours["n"] >= 10 and base["n"] >= 10:
        edge = ours["avg"] - base["avg"]
    no_green = bt["span_days"] >= cfg["no_green_days"] and bt["greens"] == 0
    top_fail = bt["fails"][0][0] if bt["fails"] else None
    return {
        "reliable": reliable, "measured": bt["measured"], "target": bt["target"],
        "edge": edge, "beats_baseline": None if edge is None else edge > 0,
        "retained": retained, "relaxed": relaxed, "min_retained": cfg["min_retained"],
        "no_green": no_green, "top_fail": top_fail, "top_fail_label": CHECK_LABELS.get(top_fail, top_fail),
        "trust": reliable and edge is not None and edge > 0,
    }


def _summary(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0, "avg": None, "wins": 0, "total": None}
    total = 1.0
    for v in vals:
        total *= 1 + v / 100
    return {"n": len(vals), "avg": sum(vals) / len(vals), "wins": sum(1 for v in vals if v > 0),
            "total": (total - 1) * 100}


def backtest_data(cfg, con):
    bt_table(con)
    cost = cfg["assumed_costs_pct"]
    rows = []
    for slot, mint, sym, level, n, passed, entry, p6, p24, bm, bs, be, bp6, bp24 in con.execute(
            "SELECT slot, mint, sym, level, n, passed, entry, p6, p24, b_mint, b_sym, b_entry, b_p6, b_p24 "
            "FROM backtest WHERE slot>=? ORDER BY slot", (time.time() - cfg["backtest_days"] * 86400,)):
        def perf(p, e):
            return (p / e - 1) * 100 - cost if (p and p > 0 and e and e > 0) else None
        rows.append({"slot": slot, "mint": mint, "sym": sym, "level": level, "n": n, "passed": passed,
                     "entry": entry, "perf6": perf(p6, entry), "perf24": perf(p24, entry),
                     "b_mint": bm, "b_sym": bs, "b_perf6": perf(bp6, be), "b_perf24": perf(bp24, be)})
    fail_count = {}
    for (fl,) in con.execute("SELECT fails FROM backtest WHERE slot>=? AND mint IS NOT NULL AND fails IS NOT NULL",
                             (time.time() - cfg["backtest_days"] * 86400,)):
        for k in (fl or "").split(","):
            if k:
                fail_count[k] = fail_count.get(k, 0) + 1
    half = len(rows) // 2

    def pick(r, key):
        return r[key] if r[key] is not None else None
    main_key = "perf24" if any(r["perf24"] is not None for r in rows) else "perf6"
    return {"rows": rows, "main": main_key,
            "all6": _summary(r["perf6"] for r in rows), "all24": _summary(r["perf24"] for r in rows),
            "go6": _summary(r["perf6"] for r in rows if r["level"] in ("green", "orange")),
            "go24": _summary(r["perf24"] for r in rows if r["level"] in ("green", "orange")),
            "base6": _summary(r["b_perf6"] for r in rows), "base24": _summary(r["b_perf24"] for r in rows),
            "half1": _summary(pick(r, main_key) for r in rows[:half]),
            "half2": _summary(pick(r, main_key) for r in rows[half:]),
            "greens": sum(1 for r in rows if r["level"] == "green"),
            "measured": sum(1 for r in rows if r["perf24"] is not None),
            "fails": sorted(fail_count.items(), key=lambda x: -x[1]),
            "target": cfg["backtest_target"], "days": cfg["backtest_days"],
            "span_days": (rows[-1]["slot"] - rows[0]["slot"]) / 86400 if rows else 0}


# --------------------------------------------------------------------------
# Règles de sortie
# --------------------------------------------------------------------------

def smart_exits(con, mint, good, since):
    """Combien de bons traders entrés sur ce token en sont (presque) sortis."""
    rows = con.execute("SELECT wallet, side, token_amount FROM trades WHERE mint=? AND ts>=?",
                       (mint, since)).fetchall()
    pos = {}
    for w, side, amt in rows:
        if w not in good:
            continue
        p = pos.setdefault(w, [0.0, 0.0])
        p[0 if side == "buy" else 1] += amt
    entered = [w for w, (b, s_) in pos.items() if b > 0]
    out = [w for w in entered if pos[w][1] >= 0.5 * pos[w][0]]
    return len(out), len(entered)


def exit_advice(cfg, pct, sold, entered):
    """Renvoie (niveau, message) ou (None, None)."""
    if pct is not None and pct <= cfg["stop_loss_pct"]:
        return "stop", f"Perte de {pct:.0f} % : ton seuil de sortie ({cfg['stop_loss_pct']} %) est atteint."
    if entered and sold / entered >= 0.5:
        return "smart", f"{sold} bon(s) trader(s) sur {entered} ont déjà revendu."
    if pct is not None and pct >= cfg["take_profit_pct"]:
        return "take", f"+{pct:.0f} % : ton objectif de gain (+{cfg['take_profit_pct']} %) est atteint."
    return None, None


# --------------------------------------------------------------------------
# Mes trades (journal perso, saisi à la main)
# --------------------------------------------------------------------------

def my_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS my_trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, opened REAL, sym TEXT, mint TEXT, usd REAL,
        entry REAL, exit REAL, closed REAL, note TEXT)""")


def my_trades(cfg, con, good=None):
    my_table(con)
    good = good or {}
    out = []
    for i, opened, sym, mint, usd, entry, ex, closed, note in con.execute(
            "SELECT * FROM my_trades ORDER BY opened DESC"):
        price = None
        if mint:
            price, _ = best_price(con, mint)
        ref = ex if ex else price
        pct = (ref / entry - 1) * 100 if (ref and entry) else None
        advice = msg = None
        sold = entered = 0
        if not ex:
            if mint:
                sold, entered = smart_exits(con, mint, good, opened - 48 * 3600)
            advice, msg = exit_advice(cfg, pct, sold, entered)
        out.append({"id": i, "opened": opened, "sym": sym, "mint": mint, "usd": usd, "entry": entry,
                    "exit": ex, "closed": closed, "note": note, "price": price, "pct": pct,
                    "pnl": usd * pct / 100 if (pct is not None and usd) else None,
                    "advice": advice, "advice_msg": msg, "smart_sold": sold, "smart_in": entered})
    done = [t for t in out if t["exit"]]
    return {"trades": out,
            "realised": sum(t["pnl"] or 0 for t in done),
            "unrealised": sum(t["pnl"] or 0 for t in out if not t["exit"]),
            "wins": sum(1 for t in done if (t["pnl"] or 0) > 0), "closed": len(done)}


def my_add(con, d):
    my_table(con)
    con.execute("INSERT INTO my_trades(opened, sym, mint, usd, entry, exit, closed, note) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (d.get("opened") or time.time(), (d.get("sym") or "?")[:40], (d.get("mint") or "").strip() or None,
                 float(d.get("usd") or 0), float(d.get("entry") or 0) or None,
                 float(d["exit"]) if d.get("exit") else None,
                 time.time() if d.get("exit") else None, (d.get("note") or "")[:200]))
    con.commit()


def my_close(con, tid, exit_price):
    my_table(con)
    con.execute("UPDATE my_trades SET exit=?, closed=? WHERE id=?", (float(exit_price), time.time(), int(tid)))
    con.commit()


def my_delete(con, tid):
    my_table(con)
    con.execute("DELETE FROM my_trades WHERE id=?", (int(tid),))
    con.commit()


# --------------------------------------------------------------------------
# Page d'un token (dans l'appli) + envoi du trade sur le téléphone
# --------------------------------------------------------------------------

def token_detail(cfg, con, mint):
    now = time.time()
    enrich(cfg, con, [mint])
    _, good, _, ranked = build_recap(cfg, con, limit=200)
    c = next((x for x in ranked if x["mint"] == mint), None)
    mk, sf = market_info(con, mint), safety_info(con, mint)
    sym = (mk or {}).get("sym") or (c or {}).get("sym") or con.execute(
        "SELECT symbol FROM trades WHERE mint=? AND symbol IS NOT NULL LIMIT 1", (mint,)).fetchone()
    if isinstance(sym, tuple):
        sym = sym[0]
    gw = list(good)
    buys = sells = []
    if gw:
        q = ",".join("?" * len(gw))
        buys = con.execute(f"SELECT ts, price, usd FROM trades WHERE mint=? AND side='buy' AND wallet IN ({q}) AND ts>=? ORDER BY ts",
                           [mint, *gw, now - 26 * 3600]).fetchall()
        sells = con.execute(f"SELECT ts, price, usd FROM trades WHERE mint=? AND side='sell' AND wallet IN ({q}) AND ts>=? ORDER BY ts",
                            [mint, *gw, now - 26 * 3600]).fetchall()
    n_tr, n_w, vol = con.execute("SELECT COUNT(*), COUNT(DISTINCT wallet), COALESCE(SUM(usd),0) FROM trades "
                                 "WHERE mint=? AND ts>=?", (mint, now - 24 * 3600)).fetchone()
    price, _ = best_price(con, mint)
    entry = (c or {}).get("entry")
    return {"mint": mint, "sym": sym or mint[:4] + "…", "market": mk, "safety": sf, "price": price,
            "candles": fetch_ohlcv(con, mint), "smart_buys": [list(x) for x in buys], "smart_sells": [list(x) for x in sells],
            "observed": {"trades": n_tr, "wallets": n_w, "usd": vol},
            "candidate": bool(c), "entry": entry, "n_good": (c or {}).get("n", 0),
            "plan": trade_plan(cfg, price, entry) if c else None,
            "fomo_url": cfg["fomo_token_url"].replace("{mint}", mint) if cfg.get("fomo_token_url") else None}


def send_take(cfg, con, mint):
    d = token_detail(cfg, con, mint)
    p = d["plan"] or trade_plan(cfg, d["price"], None)
    bt = backtest_data(cfg, con)
    lines = []
    if bt["measured"] < bt["target"]:
        lines += [f"PHASE DE TEST : backtest {bt['measured']}/{bt['target']} créneaux, pas encore fiable. Mise minimale ou observe.", ""]
    lines += [f"PRENDRE LE TRADE : {d['sym']}", "",
             "Adresse du token (appui long pour copier) :", mint, ""]
    if p:
        lines += [f"Prix actuel : {d['price']:.10g} $" if d["price"] else "",
                  f"Zone d'achat : {p['zone_low']:.10g} - {p['zone_high']:.10g} $" if d["plan"] else "",
                  f"Stop ({p['stop_pct']} %) : {p['stop']:.10g} $",
                  f"Objectif 1 (+{p['t1_pct']:.0f} %, vendre la moitié) : {p['target1']:.10g} $",
                  f"Objectif 2 (+{p['t2_pct']:.0f} %, vendre le reste) : {p['target2']:.10g} $", ""]
    if d.get("fomo_url"):
        lines += ["Ouvrir dans Fomo : " + d["fomo_url"], ""]
    else:
        lines += ["Dans Fomo : loupe (recherche) -> colle l'adresse -> Acheter.", ""]
    lines += ["Petite mise seulement. C'est toi qui décides."]
    send(cfg, "\n".join(x for x in lines if x is not None))
    return d["sym"]


# --------------------------------------------------------------------------
# Données pour l'application (tableau de bord)
# --------------------------------------------------------------------------

def dashboard_data(cfg, con):
    now = time.time()
    stats, good, rejected, ranked, positions = enriched_recap(cfg, con)
    top = ranked[: int(cfg["max_candidates"])]
    n_trades, oldest, newest = con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM trades").fetchone()
    hist_days = (now - oldest) / 86400 if oldest else 0
    track_n, track_avg = tracking_stats(cfg, con)

    for c in top:
        c["change"] = (c["price"] / c["entry"] - 1) * 100 if c["price"] and c["entry"] else None

    signal = build_signal(cfg, con, top, hist_days, now)
    if signal.get("checks"):
        signal["passed"] = sum(1 for k in signal["checks"] if k["ok"])
        signal["total"] = len(signal["checks"])
    # traders à copier sur le trade du jour
    if signal.get("candidate"):
        mint = signal["candidate"]["mint"]
        copy = []
        me = (cfg.get("my_wallet") or "").strip()
        for w, plist in positions.items():
            for p in plist:
                if p["mint"] == mint and w in good and w != me:
                    r = trader_row(w, good[w], stats[w])
                    r.update({"entry": p["entry"], "usd": p["usd"], "ts": p["ts"]})
                    copy.append(r)
        copy.sort(key=lambda r: -r["score"])
        signal["copy"] = copy
        sold, entered = smart_exits(con, mint, good, now - 48 * 3600)
        signal["smart_sold"], signal["smart_in"] = sold, entered
        signal["market"] = market_info(con, mint)
        signal["safety"] = safety_info(con, mint)
        signal["candles"] = fetch_ohlcv(con, mint)
        signal["plan"] = trade_plan(cfg, signal["candidate"].get("price"), signal["candidate"].get("entry"))
        gw = [w for w in good]
        sells = []
        if gw:
            q = ",".join("?" * len(gw))
            sells = con.execute(f"SELECT ts, price, usd FROM trades WHERE mint=? AND side='sell' AND wallet IN ({q}) "
                                f"AND ts>=? ORDER BY ts", [mint, *gw, now - 26 * 3600]).fetchall()
        signal["smart_sells"] = [list(x) for x in sells]

    mine_w = (cfg.get("my_wallet") or "").strip()
    good_others = {w: sc for w, sc in good.items() if w != mine_w}
    board = trader_board(cfg, con, stats, good_others, ranked, positions, hist_days, track_n, track_avg, now)
    try:
        snapshot_ranks(con, good_others)
    except sqlite3.OperationalError:
        pass  # base occupée par une collecte : on réessaiera au prochain rafraîchissement
    ch = rank_changes(con, good_others)
    order = {w: i for i, (w, _) in enumerate(sorted(good_others.items(), key=lambda x: -x[1])[:60], 1)}
    for b in board:
        b["rank_score"] = order.get(b["wallet"])
        b["prev_rank"] = ch["prev"].get(b["wallet"])
        b["is_new"] = bool(ch["since"]) and b["wallet"] not in ch["prev"]
    watch_table(con)
    tracking = {"tracked": con.execute("SELECT COUNT(*) FROM watch").fetchone()[0],
                "requests": int(float(meta_get(con, "last_rpc_requests", 0) or 0)),
                "rank_since": ch["since"], "gone": ch["gone"]}

    picks = []
    for day, rank, mint, sym, p0, score in con.execute(
            "SELECT day, rank, mint, symbol, price, score FROM picks ORDER BY day DESC, rank LIMIT 60"):
        p1, _ = best_price(con, mint)
        perf = (p1 / p0 - 1) * 100 - cfg["assumed_costs_pct"] if (p0 and p1) else None
        picks.append({"day": day, "rank": rank, "mint": mint, "symbol": sym,
                      "price": p0, "now": p1, "perf": perf})

    hourly = []
    h0 = int(now // 3600) * 3600
    for i in range(23, -1, -1):
        a = h0 - i * 3600
        cnt, vol = con.execute("SELECT COUNT(*), COALESCE(SUM(usd),0) FROM trades "
                               "WHERE ts>=? AND ts<?", (a, a + 3600)).fetchone()
        hourly.append({"t": a, "count": cnt, "usd": vol})

    hot = []
    since = now - 6 * 3600
    for mint, sym, buys, wallets, usd in con.execute(
            "SELECT mint, symbol, COUNT(*), COUNT(DISTINCT wallet), SUM(usd) FROM trades "
            "WHERE side='buy' AND ts>=? GROUP BY mint ORDER BY SUM(usd) DESC LIMIT 12", (since,)):
        first = con.execute("SELECT price FROM trades WHERE mint=? AND ts>=? AND price>0 "
                            "ORDER BY ts LIMIT 1", (mint, since)).fetchone()
        last, _ = best_price(con, mint)
        ch = (last / first[0] - 1) * 100 if (first and first[0] and last) else None
        hot.append({"mint": mint, "symbol": sym, "buys": buys, "wallets": wallets,
                    "usd": usd, "price": last, "change": ch})

    today = datetime.now().strftime("%Y-%m-%d")
    greens_today = con.execute("SELECT COUNT(*) FROM meta WHERE k LIKE ?", (f"alert_{today}_%",)).fetchone()[0]

    return {
        "now": now, "signal": signal, "greens_today": greens_today,
        "trades": n_trades, "history_days": hist_days,
        "newest_trade": newest,
        "last_collect_run": float(meta_get(con, "last_collect_run", 0) or 0),
        "analysed": len(stats), "retained": len(good),
        "rejected": sorted(([k, v] for k, v in rejected.items()), key=lambda x: -x[1]),
        "candidates": top, "board": board[:60], "picks": picks,
        "hourly": hourly, "hot": hot,
        "backtest": (bt_d := backtest_data(cfg, con)),
        "verdict": verdicts(cfg, bt_d, len(good), bool(cfg.get("_relaxed"))),
        "mine": my_trades(cfg, con, good),
        "my_wallet": mine_w,
        "my_chain": wallet_detail(cfg, con, mine_w) if mine_w else None,
        "tracking": tracking,
        "exit_rules": {"stop": cfg["stop_loss_pct"], "take": cfg["take_profit_pct"],
                       "min_liq": cfg["min_liquidity_usd"]},
        "track_n": track_n, "track_avg": track_avg,
        "cloud": {"repo": (cfg.get("cloud_repo") or "").strip(),
                  "last_run": float(meta_get(con, "cloud_last_run", 0) or 0),
                  "pulled": float(meta_get(con, "cloud_pulled", 0) or 0),
                  "error": meta_get(con, "cloud_error", "") or "",
                  "alive": cloud_alive(cfg, con)},
    }


# --------------------------------------------------------------------------
# Cloud GitHub : collecte 24 h/24 même PC éteint
# --------------------------------------------------------------------------
# Sur GitHub Actions (gratuit), toutes les 30 min : collecte, backtest, alertes feu vert,
# récap du matin. La base est rangée sur la branche "data" du dépôt. Le PC la récupère et la
# fusionne avec la sienne (ton journal et ton wallet restent uniquement sur ton PC).

CLOUD_TABLES = ("trades", "picks", "market", "safety", "ohlcv", "ohlcv_hist", "watch", "backtest")
LOCAL_META = ("collect_lock", "cloud_etag", "cloud_pulled", "cloud_error", "mine_last_run")


def paris_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Paris"))
    except Exception:
        return datetime.now(timezone.utc) + timedelta(hours=1)


def cloud_run(cfg, con):
    """Une passe dans le cloud (commande : python fomo_recap.py cloud)."""
    t0 = time.time()
    meta_set(con, "collect_lock", 0)  # une passe précédente coupée net ne doit pas bloquer
    try:
        collect(cfg, con)
    except Exception as e:
        print("Collecte impossible :", e)
    done = 0
    try:
        while time.time() - t0 < float(cfg.get("cloud_backtest_until_s", 1260)):
            n = run_backtest(cfg, con, max_new=4)
            if not n:
                break
            done += n
    except Exception as e:
        print("Backtest :", e)
    print(f"Backtest : {done} calculs.")
    try:
        sym = check_alerts(cfg, con, exits=False)
        if sym:
            print("Alerte feu vert envoyée :", sym)
    except Exception as e:
        print("Alerte impossible :", e)
    now_p = paris_now()
    day = now_p.strftime("%Y-%m-%d")
    if now_p.hour >= int(cfg.get("recap_hour", 8)) and meta_get(con, "cloud_recap_day") != day:
        try:
            send(cfg, recap_text(cfg, con, save=True))
            meta_set(con, "cloud_recap_day", day)
            print("Récap du matin envoyé.")
        except Exception as e:
            print("Récap impossible :", e)
    meta_set(con, "cloud_last_run", time.time())
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(f"Passe cloud terminée en {time.time() - t0:.0f} s.")


def cloud_seed(cfg, con, out_path):
    """Copie de ta base pour démarrer le cloud avec l'historique déjà collecté,
    SANS ton wallet ni ton journal (le dépôt est public)."""
    import gzip
    import shutil
    mine = (cfg.get("my_wallet") or "").strip()
    tmp = out_path + ".tmp.sqlite"
    if os.path.exists(tmp):
        os.remove(tmp)
    con.commit()
    con.execute("VACUUM INTO ?", (tmp,))
    c2 = sqlite3.connect(tmp)
    if mine:
        c2.execute("DELETE FROM trades WHERE wallet=?", (mine,))
        c2.execute("DELETE FROM watch WHERE wallet=?", (mine,))
        c2.execute("DELETE FROM rank_snap WHERE wallet=?", (mine,))
    c2.execute("DROP TABLE IF EXISTS my_trades")
    c2.execute("DELETE FROM meta WHERE k LIKE 'exit_%' OR k IN (" + ",".join("?" * len(LOCAL_META)) + ")", LOCAL_META)
    c2.commit()
    c2.execute("VACUUM")
    c2.close()
    with open(tmp, "rb") as fi, gzip.open(out_path, "wb", compresslevel=9) as fo:
        shutil.copyfileobj(fi, fo)
    os.remove(tmp)
    return os.path.getsize(out_path)


def cloud_pull(cfg, con, force=False):
    """Récupère la base du cloud et la fusionne. Renvoie un petit texte d'état."""
    import gzip
    repo = (cfg.get("cloud_repo") or "").strip().strip("/")
    if not repo:
        return None
    url = f"https://raw.githubusercontent.com/{repo}/data/fomo_data.sqlite.gz"
    headers = {"User-Agent": "FomoRecap/1.0", "Cache-Control": "no-cache"}
    etag = meta_get(con, "cloud_etag")
    if etag and not force:
        headers["If-None-Match"] = etag
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
            blob = r.read()
            new_etag = r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            meta_set(con, "cloud_pulled", time.time())
            return "déjà à jour"
        msg = "pas encore de données dans le cloud" if e.code == 404 else f"GitHub HTTP {e.code}"
        meta_set(con, "cloud_error", msg)
        return msg
    except Exception as e:
        meta_set(con, "cloud_error", f"GitHub injoignable ({e})")
        return "GitHub injoignable"
    tmp = DB_PATH + ".cloud"
    with open(tmp, "wb") as f:
        f.write(gzip.decompress(blob))
    ext_tables(con)
    watch_table(con)
    bt_table(con)
    con.execute("CREATE TABLE IF NOT EXISTS ohlcv_hist(mint TEXT PRIMARY KEY, data TEXT, fetched REAL)")
    con.commit()
    mine = (cfg.get("my_wallet") or "").strip()
    con.execute("ATTACH DATABASE ? AS c", (tmp,))
    try:
        cloud_tabs = {r[0] for r in con.execute("SELECT name FROM c.sqlite_master WHERE type='table'")}
        for t in CLOUD_TABLES:
            if t not in cloud_tabs:
                continue
            mine_cols = [r[1] for r in con.execute(f"PRAGMA main.table_info({t})")]
            theirs = {r[1] for r in con.execute(f"PRAGMA c.table_info({t})")}
            cols = ",".join(c for c in mine_cols if c in theirs)
            if t in ("market", "safety", "ohlcv", "ohlcv_hist"):  # on garde la version la plus fraîche
                con.execute(f"INSERT OR REPLACE INTO main.{t}({cols}) SELECT {cols} FROM c.{t} AS x "
                            f"WHERE NOT EXISTS (SELECT 1 FROM main.{t} m WHERE m.mint=x.mint AND m.fetched >= x.fetched)")
                continue
            if t == "picks":  # le cloud fait le récap du matin : ses choix du jour font foi
                con.execute("DELETE FROM main.picks WHERE day IN (SELECT day FROM c.picks)")
            if t == "watch" and mine:
                con.execute(f"INSERT OR REPLACE INTO main.watch({cols}) SELECT {cols} FROM c.watch WHERE wallet<>?", (mine,))
                con.execute("DELETE FROM main.watch WHERE wallet<>? AND wallet NOT IN (SELECT wallet FROM c.watch)", (mine,))
                continue
            if t == "watch":
                con.execute("DELETE FROM main.watch WHERE wallet NOT IN (SELECT wallet FROM c.watch)")
            con.execute(f"INSERT OR REPLACE INTO main.{t}({cols}) SELECT {cols} FROM c.{t}")
        if "rank_snap" in cloud_tabs:
            con.execute("DELETE FROM main.rank_snap")
            con.execute("INSERT INTO main.rank_snap SELECT ts, wallet, rank, score FROM c.rank_snap")
        q = ",".join("?" * len(LOCAL_META))
        con.execute(f"INSERT OR REPLACE INTO main.meta SELECT k, v FROM c.meta "
                    f"WHERE k NOT LIKE 'exit_%' AND k NOT IN ({q})", LOCAL_META)
        con.commit()
    finally:
        con.execute("DETACH DATABASE c")
        try:
            os.remove(tmp)
        except OSError:
            pass
    if new_etag:
        meta_set(con, "cloud_etag", new_etag)
    meta_set(con, "cloud_pulled", time.time())
    meta_set(con, "cloud_error", "")
    return "données du cloud récupérées"


def cloud_alive(cfg, con):
    last = float(meta_get(con, "cloud_last_run", 0) or 0)
    return bool((cfg.get("cloud_repo") or "").strip()) and time.time() - last < float(cfg["cloud_stale_hours"]) * 3600


def pc_collect(cfg, con, verbose=True):
    """Ce que fait le PC à chaque collecte. Avec le cloud : on récupère ses données, on lit
    seulement ton wallet et on surveille tes sorties. Sans cloud (ou cloud en panne) : tout."""
    if (cfg.get("cloud_repo") or "").strip():
        state = cloud_pull(cfg, con)
        if cloud_alive(cfg, con):
            n = 0
            try:
                n = collect_solana(cfg, con, verbose=False, only_mine=True)
            except Exception as e:
                if verbose:
                    print("Ton wallet :", e)
            try:
                check_alerts(cfg, con, green=False)
            except Exception as e:
                if verbose:
                    print("Alerte impossible :", e)
            if verbose:
                print(f"Mode cloud : {state}. Ton wallet : {n} trades ajoutés.")
            return {"cloud": True, "state": state, "added": n}
        if verbose:
            print(f"Cloud muet depuis plus de {cfg['cloud_stale_hours']} h ({state}) : le PC collecte lui-même.")
    n = collect(cfg, con, verbose=verbose)
    sym = None
    try:
        sym = check_alerts(cfg, con)
        if sym and verbose:
            print("Alerte feu vert envoyée :", sym)
    except Exception as e:
        if verbose:
            print("Alerte impossible :", e)
    return {"cloud": False, "added": n, "sym": sym}


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def telegram(cfg, method, params=None):
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/{method}"
    body = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def send(cfg, text):
    if not cfg.get("telegram_chat_id"):
        raise RuntimeError("telegram_chat_id vide dans config.json (lance : run.bat telegram-id)")
    for i in range(0, len(text), 3900):
        telegram(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"],
                                      "text": text[i:i + 3900],
                                      "disable_web_page_preview": True})


# --------------------------------------------------------------------------
# Commandes
# --------------------------------------------------------------------------

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd in ("help", "-h", "--help"):
        print(__doc__)
        return
    cfg = load_config()
    con = db()

    if cmd == "telegram-id":
        res = telegram(cfg, "getUpdates")
        ids = {(u.get("message") or {}).get("chat", {}).get("id") for u in res.get("result", [])}
        ids.discard(None)
        if not ids:
            print("Aucun message trouvé. Écris d'abord 'salut' à ton bot sur Telegram, puis relance.")
        else:
            print("Ton chat_id :", ", ".join(str(i) for i in ids))
            print("Copie-le dans config.json -> \"telegram_chat_id\"")
        return

    if cmd == "check" and cfg.get("data_source", "solana") != "bitquery":
        print("1/2 Nœud public Solana (gratuit)...")
        rpc = Rpc(cfg["solana_rpc"], cfg["rpc_min_interval"], 30)
        sigs = [x for x in rpc.sigs(cfg["fomo_signer"], 50) if not x.get("err")]
        found = 0
        price_sol = sol_usd(cfg, con)
        for x in sigs[:10]:
            if parse_swap(rpc.tx(x["signature"]), cfg["fomo_signer"], price_sol):
                found += 1
        print(f"   OK, {found} achats/ventes Fomo lus sur 10 transactions récentes.")
        print("2/2 Telegram...")
        send(cfg, "Test du récap Fomo : ça marche (source gratuite) !")
        print("   OK, message envoyé. Tout est prêt.")
        return

    if cmd == "check":
        print("1/2 Bitquery...")
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = bitquery(cfg, TRADES_QUERY, {"signer": cfg["fomo_signer"], "protocol": cfg["fomo_protocol"],
                                            "since": since, "limit": 5, "minusd": "0"})
        rows = data["Solana"]["DEXTrades"]
        print(f"   OK, {len(rows)} trades Fomo trouvés dans la dernière heure (échantillon).")
        if not rows:
            print("   Rien trouvé : l'adresse Fomo a peut-être changé. Envoie ce message à Claude.")
        print("2/2 Telegram...")
        send(cfg, "Test du récap Fomo : ça marche !")
        print("   OK, message envoyé. Tout est prêt.")
        return

    if cmd == "sample":
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = bitquery(cfg, TRADES_QUERY, {"signer": cfg["fomo_signer"], "protocol": cfg["fomo_protocol"],
                                            "since": since, "limit": 5, "minusd": "0"})
        for r in data["Solana"]["DEXTrades"]:
            print(json.dumps(r, indent=1)[:1500])
            print("->", parse_trade(r))
        return

    if cmd == "collect":
        pc_collect(cfg, con)
        return

    if cmd == "cloud":
        cloud_run(cfg, con)
        return

    if cmd == "cloud-seed":
        out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "fomo-cloud", "seed", "fomo_data.sqlite.gz")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        size = cloud_seed(cfg, con, out)
        print(f"Base de départ prête ({size / 1e6:.1f} Mo, sans ton wallet ni ton journal) :\n  {out}")
        return

    if cmd == "cloud-pull":
        print("Cloud :", cloud_pull(cfg, con, force=True) or "pas de cloud_repo dans config.json")
        return

    if cmd in ("recap", "recap-test"):
        res = {"cloud": False}
        try:
            res = pc_collect(cfg, con, verbose=False)
        except Exception as e:
            print("Collecte impossible avant le récap :", e)
        text = recap_text(cfg, con, save=(cmd == "recap" and not res.get("cloud")))
        print(text)
        if cmd == "recap":
            if res.get("cloud"):
                print("\nMode cloud : c'est GitHub qui envoie le récap du matin (pas de doublon).")
            else:
                send(cfg, text)
                print("\nRécap envoyé sur Telegram.")
        return

    print("Commande inconnue. Lance : run.bat help")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERREUR :", e)
        sys.exit(1)
