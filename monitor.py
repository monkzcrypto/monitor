"""
IOF / Institutional Oil Fund token monitor.

Checks these places every run and combines them into one list, so the same
token is never counted twice:
  1. DexScreener    - onchain tokens on every DEX, including PumpSwap, Raydium,
                      Meteora, Uniswap and Aerodrome (what shows up in the
                      Coinbase app's search bar).
  2. GeckoTerminal  - a second onchain source (search + newest pools).
  3. pump.fun       - catches coins the moment they bond to PumpSwap.
  4. Coinbase       - Coinbase's official exchange listings.

Sends a phone notification (via the free ntfy app) whenever a token
matching the name or ticker below has BONDED (graduated off its launchpad's
bonding curve onto a real exchange) and hasn't been alerted on before.
A token still on its bonding curve is ignored until it bonds.

It EMAILS the report straight to Blockaid support from your Gmail.

Report wording (how you like it now):
  - No bundling, wash trading or holder-spoofing claims.
  - Market cap only when the monitor actually has the number.
  - Tokens under $50K market cap are reported as already rugged.
  - Every report asks 4 times to flag it as MALICIOUS and twice to get it
    off Coinbase search.

ONE follow-up: if a reported fake is still up 1 hour after the first email,
it sends one more email as a reply in the same thread ("second time
reporting"). After that it never emails about that token again.

The real token below is never alerted on or reported.
Base addresses (0x...) are compared without caring about capital letters.
"""

import datetime
import re
import time
import json
import os
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import make_msgid

# ---- What to look for (you can edit these) ---------------------------------
SYMBOL_MATCH = "IOF"                    # any ticker or name CONTAINING this counts
NAME_MATCH = "institutional oil fund"   # any name containing this (or close spellings) counts
SEARCH_TERMS = ["IOF", "$IOF", "Institutional Oil Fund", "Institutional Oil",
                "Oil Fund", "IOF pump", "IOF pumpswap", "IOF solana", "IOF base"]

# DexScreener / GeckoTerminal labels for launchpad bonding curves. A token only
# counts as bonded once it trades somewhere NOT on this list, with real
# liquidity. (PumpSwap is NOT on this list, so PumpSwap tokens count as bonded.)
BONDING_CURVE_DEXES = {"pumpfun", "moonshot", "launchlab", "boop", "believe",
                       "bonk", "letsbonk", "heaven", "jupstudio", "dbc",
                       "pump-fun", "pump_fun", "raydium-launchlab", "meteora-dbc",
                       "boop-fun", "moonshot-solana", "believe-app", "letsbonk-fun"}

GECKO_NETWORKS = ["solana", "base"]
GECKO_TERMS = ["IOF", "Institutional Oil Fund"]
GECKO_PAGES = 2                         # pages of search results per term
NEW_POOL_PAGES = 2                      # pages of brand-new pools per chain
QUOTE_SYMBOLS = {"SOL", "WSOL", "WETH", "ETH", "USDC", "USDT", "VIRTUAL", "ZORA", "CLANKER"}

# The REAL token. It is never alerted on and never gets a report.
LEGIT_CAS = {"2sY7rkMCQyFNcSpHm3fciJf2ptYRg3cYpn4srN6Bpump"}

# How long ago the REAL token launched, as written in the reports. Edit this one line to update it.
REAL_TOKEN_AGE = "over 3 weeks ago"

# Tokens below this market cap are reported as "already rugged".
RUGGED_MCAP = 50_000

REPORT_URL = "https://report.blockaid.io/scam"

# Only ping for tokens that bonded within this many minutes (i.e. "right when it bonds").
BOND_WINDOW_MINUTES = 60
ALERT_CHAINS = {"solana", "base"}

# ---- Automatic email reports to Blockaid (you can edit these) --------------
EMAIL_REPORTS = True                    # set to False to turn emailing off
BLOCKAID_EMAIL = "support@blockaid.co"
DAILY_EMAIL_LIMIT = 100                 # never send more than this many per day (follow-ups count)
EMAIL_ONLY_CLOSE_COPIES = True          # loose matches only ping your phone
FOLLOW_UP = True                        # set to False to turn the 1-hour follow-up off
FOLLOW_UP_AFTER_MINUTES = 60            # wait this long after the first report
FOLLOW_UP_GIVE_UP_HOURS = 6             # if the monitor was paused, don't follow up on old ones
# ----------------------------------------------------------------------------

STATE_FILE = "seen.json"
MODE = "bonded-v4"                      # unchanged on purpose, so your saved history is kept
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()


def same_ca(addr):
    """Base/EVM addresses (0x...) ignore capital letters. Solana ones don't."""
    a = str(addr or "").strip()
    return a.lower() if a.lower().startswith("0x") else a


def norm_key(key):
    prefix, sep, addr = str(key).rpartition(":")
    return f"{prefix}:{same_ca(addr)}" if sep else key


def is_legit(addr):
    return same_ca(addr) in {same_ca(a) for a in LEGIT_CAS}


def get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "iof-monitor/1.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def letters_only(text):
    return re.sub(r"[^a-z]", "", (text or "").lower())


def name_matches(name):
    letters = letters_only(name)
    target = re.sub(r"[^a-z]", "", NAME_MATCH)
    return target in letters or ("instit" in letters and "oilfund" in letters)


def exact_ticker(symbol):
    return letters_only(symbol) == SYMBOL_MATCH.lower()


def matches(name, symbol):
    key = SYMBOL_MATCH.lower()
    n = name or ""
    return bool(key in letters_only(symbol)
                or SYMBOL_MATCH in n
                or re.search(r"(?i)(?<![a-z])i[\s.\-_/]*o[\s.\-_/]*f(?![a-z])", n)
                or name_matches(n))


def as_number(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def iso_to_ms(text):
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


def is_bonded_pair(p):
    dex = (p.get("dexId") or "").lower()
    liq = (p.get("liquidity") or {}).get("usd") or 0
    return dex not in BONDING_CURVE_DEXES and liq > 0


def token_entry(source, name, symbol, chain, address, url, liq, created_ms, mcap, bonded_ms=None):
    return {
        "bonded_ms": bonded_ms,
        "source": source, "name": name, "symbol": symbol, "chain": chain,
        "address": address, "url": url, "liquidity_usd": liq,
        "created_ms": created_ms, "mcap": mcap,
        "name_match": name_matches(name), "exact_ticker": exact_ticker(symbol),
        "reportable": True,
    }


# ---- Sources ---------------------------------------------------------------

def check_dexscreener():
    tokens = {}
    for term in SEARCH_TERMS:
        url = "https://api.dexscreener.com/latest/dex/search?q=" + urllib.parse.quote(term)
        try:
            data = get_json(url)
        except Exception as e:
            print(f"DexScreener skipped '{term}': {e}")
            continue
        for p in data.get("pairs") or []:
            bt = p.get("baseToken") or {}
            if not matches(bt.get("name"), bt.get("symbol")) or is_legit(bt.get("address")):
                continue
            key = norm_key(f"dex:{p.get('chainId', '?')}:{bt.get('address', '?')}")
            tokens.setdefault(key, {"info": bt, "pairs": {}})
            tokens[key]["pairs"][p.get("pairAddress") or p.get("url")] = p

    found = {}
    for key, t in tokens.items():
        bonded = [p for p in t["pairs"].values() if is_bonded_pair(p)]
        if not bonded:
            continue  # still on its bonding curve; check again next run
        best = max(bonded, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        created = [p.get("pairCreatedAt") for p in t["pairs"].values() if p.get("pairCreatedAt")]
        bonded_at = [p.get("pairCreatedAt") for p in bonded if p.get("pairCreatedAt")]
        found[key] = token_entry(
            f"Onchain - bonded, trading on {best.get('dexId')}",
            t["info"].get("name"), t["info"].get("symbol"), best.get("chainId", "?"),
            t["info"].get("address", "?"), best.get("url"),
            (best.get("liquidity") or {}).get("usd"),
            min(created) if created else None, best.get("marketCap") or best.get("fdv"),
            bonded_ms=min(bonded_at) if bonded_at else None)
    return found


def gecko_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "iof-monitor/1.0", "Accept": "application/json;version=20230302"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception as e:
        print(f"GeckoTerminal skipped {url}: {e}")
        data = None
    time.sleep(3)  # stay under the free rate limit
    return data


def gecko_pools(data, network, found):
    """Pulls matching bonded tokens out of one GeckoTerminal response."""
    tokens = {i["id"]: i.get("attributes", {})
              for i in data.get("included", []) if i.get("type") == "token"}
    pools = data.get("data") or []
    for pool in pools:
        attr = pool.get("attributes", {})
        rel = pool.get("relationships", {})
        dex = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
        liq = as_number(attr.get("reserve_in_usd")) or 0
        if dex.lower() in BONDING_CURVE_DEXES or liq <= 0:
            continue  # not bonded yet
        for side in ("base_token", "quote_token"):
            tid = ((rel.get(side) or {}).get("data") or {}).get("id")
            tok = tokens.get(tid) or {}
            sym, name, addr = tok.get("symbol"), tok.get("name"), tok.get("address")
            if not addr or (sym or "").upper() in QUOTE_SYMBOLS:
                continue
            if is_legit(addr) or not matches(name, sym):
                continue
            key = norm_key(f"dex:{network}:{addr}")
            created = iso_to_ms(attr.get("pool_created_at") or "")
            mcap = as_number(attr.get("market_cap_usd")) or as_number(attr.get("fdv_usd"))
            old = found.get(key)
            if old and (old.get("liquidity_usd") or 0) >= liq:
                continue
            found[key] = token_entry(
                f"Onchain - bonded, trading on {dex} (GeckoTerminal)", name, sym,
                network, addr,
                f"https://www.geckoterminal.com/{network}/pools/{attr.get('address')}",
                liq, created, mcap, bonded_ms=created)
    return len(pools)


def check_geckoterminal():
    found = {}
    for network in GECKO_NETWORKS:
        for term in GECKO_TERMS:
            for page in range(1, GECKO_PAGES + 1):
                data = gecko_get("https://api.geckoterminal.com/api/v2/search/pools?query="
                                 + urllib.parse.quote(term) + f"&network={network}&page={page}"
                                 + "&include=base_token,quote_token,dex")
                if not data or gecko_pools(data, network, found) < 20:
                    break  # error or no more pages
    return found


def check_new_pools():
    """Bonus: scans the newest pools on every DEX (PumpSwap included) for matches."""
    found = {}
    for network in GECKO_NETWORKS:
        for page in range(1, NEW_POOL_PAGES + 1):
            data = gecko_get(f"https://api.geckoterminal.com/api/v2/networks/{network}/new_pools"
                             f"?page={page}&include=base_token,quote_token,dex")
            if not data:
                break
            gecko_pools(data, network, found)
    return found


def check_pumpfun():
    """pump.fun's unofficial website data. Bonded ("complete") coins move to
    PumpSwap. If pump.fun blocks this, it's skipped and the rest keeps working."""
    urls = []
    for term in GECKO_TERMS:
        q = urllib.parse.quote(term)
        urls += [
            f"https://frontend-api-v3.pump.fun/coins?searchTerm={q}&offset=0&limit=50"
            "&sort=created_timestamp&order=DESC&includeNsfw=true",
            f"https://frontend-api-v3.pump.fun/coins/search?searchTerm={q}&offset=0&limit=50"
            "&sort=created_timestamp&order=DESC&includeNsfw=true",
        ]
    found, worked = {}, False
    for url in urls:
        try:
            data = get_json(url)
        except Exception:
            continue
        worked = True
        coins = data if isinstance(data, list) else (data.get("coins") or data.get("data") or [])
        for c in coins:
            if not isinstance(c, dict) or not c.get("complete"):
                continue  # only bonded coins
            mint, name, sym = c.get("mint"), c.get("name"), c.get("symbol")
            if not mint or is_legit(mint) or not matches(name, sym):
                continue
            created = c.get("created_timestamp")
            found[f"dex:solana:{mint}"] = token_entry(
                "Onchain - bonded to PumpSwap (pump.fun)", name, sym, "solana", mint,
                f"https://pump.fun/coin/{mint}", None,
                created if isinstance(created, (int, float)) else None,
                as_number(c.get("usd_market_cap")))
    if not worked:
        print("pump.fun was unreachable this run (skipped)")
    return found


def check_coinbase_official():
    found = {}
    for c in get_json("https://api.exchange.coinbase.com/currencies"):
        if matches(c.get("name"), c.get("id")):
            found[f"coinbase:{c.get('id')}"] = {
                "source": "Coinbase official listing",
                "name": c.get("name"), "symbol": c.get("id"),
                "chain": "Coinbase Exchange", "address": "-",
                "url": f"https://www.coinbase.com/price/{str(c.get('id')).lower()}",
                "liquidity_usd": None,
            }
    return found


def live_pairs(ca):
    """Looks a token up directly on DexScreener. Returns (bonded pairs, all pairs),
    or None if the lookup failed."""
    try:
        data = get_json("https://api.dexscreener.com/latest/dex/tokens/" + urllib.parse.quote(ca))
    except Exception as e:
        print(f"Lookup failed for {ca}: {e}")
        return None
    pairs = [p for p in (data.get("pairs") or [])
             if same_ca((p.get("baseToken") or {}).get("address")) == same_ca(ca)]
    return [p for p in pairs if is_bonded_pair(p)], pairs


def fill_bond_times(current, keys, limit=10):
    """pump.fun doesn't say WHEN a coin bonded, so look up its PumpSwap pool."""
    for k in keys[:limit]:
        t = current[k]
        res = live_pairs(t.get("address"))
        time.sleep(0.3)
        if not res or not res[0]:
            continue
        bonded = res[0]
        times = [p.get("pairCreatedAt") for p in bonded if p.get("pairCreatedAt")]
        if times:
            t["bonded_ms"] = min(times)
            best = max(bonded, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
            t["mcap"] = t.get("mcap") or best.get("marketCap") or best.get("fdv")
            t["liquidity_usd"] = (best.get("liquidity") or {}).get("usd")
            t["url"] = best.get("url") or t.get("url")


# ---- Phone alerts ----------------------------------------------------------

def notify(title, message, click=None, priority=3, actions=None):
    limit = 3000  # ntfy rejects big messages, so long ones are sent in parts
    if len(message.encode("utf-8")) > limit:
        parts, chunk = [], ""
        for para in message.split("\n\n"):
            piece = (chunk + "\n\n" + para) if chunk else para
            if len(piece.encode("utf-8")) > limit and chunk:
                parts.append(chunk)
                chunk = para
            else:
                chunk = piece
        parts.append(chunk)
        ok = True
        for i, part in enumerate(parts, 1):
            ok = send_one(f"{title} ({i}/{len(parts)})", part[:limit * 2 // 3] if len(part.encode("utf-8")) > limit else part,
                          click if i == 1 else None, priority, actions if i == 1 else None) and ok
        return ok
    return send_one(title, message, click, priority, actions)


def send_one(title, message, click=None, priority=3, actions=None):
    if not TOPIC:
        print("NTFY_TOPIC secret is missing, so no notification was sent.")
        print(title, "-", message)
        return False
    payload = {"topic": TOPIC, "title": title, "message": message,
               "priority": priority, "tags": ["oil_drum"]}
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = actions
    req = urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
        time.sleep(1)
        return True
    except Exception as e:
        print(f"Alert failed to send ({title}): {e}")
        return False


# ---- Report wording --------------------------------------------------------

def age_short(created_ms):
    """e.g. '5 minutes ago, at 3:42 PM ET, Oct 6'"""
    if not created_ms:
        return "recently"
    now = datetime.datetime.now(datetime.timezone.utc)
    minutes = max(0, int((now.timestamp() * 1000 - created_ms) / 60000))
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins or not parts:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    try:
        from zoneinfo import ZoneInfo
        local = datetime.datetime.fromtimestamp(created_ms / 1000, ZoneInfo("America/New_York"))
        clock = local.strftime("%-I:%M %p ET, %b %-d")
    except Exception:
        clock = datetime.datetime.fromtimestamp(created_ms / 1000, datetime.timezone.utc).strftime("%H:%M UTC, %b %d")
    return f"{' and '.join(parts)} ago, at {clock}"


def known_mcap(mcap):
    return isinstance(mcap, (int, float)) and mcap > 0


def money(mcap):
    for size, label in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if mcap >= size:
            return f"${mcap / size:,.1f}{label}".replace(".0" + label, label)
    return f"${mcap:,.0f}"


def is_rugged(mcap):
    return known_mcap(mcap) and mcap < RUGGED_MCAP


def copied_what(t):
    if t.get("exact_ticker") and t.get("name_match"):
        return "ticker, name, and pfp"
    if t.get("exact_ticker"):
        return "ticker and pfp"
    if t.get("name_match"):
        return "name and pfp"
    return f"branding (they launched as \"{t.get('name')}\" with the ticker ${t.get('symbol')})"


def chain_label(t):
    return "Solana" if str(t.get("chain", "")).lower() == "solana" else "BASE chain"


def report_text(t, mcap=None, second=False):
    """The report. second=True is the 1-hour follow-up."""
    ca = t["address"]
    real = next(iter(LEGIT_CAS))
    mcap = t.get("mcap") if mcap is None else mcap
    rugged = is_rugged(mcap)
    copied = copied_what(t)
    where = chain_label(t)
    solana = where == "Solana"

    if second:
        opening = (f"This is the second time I am reporting this CA on {where} as a MALICIOUS "
                   "token, and it is still up and trading.")
    else:
        opening = f"I am writing to report the following token CA on {where} as a MALICIOUS token."

    if rugged:
        story = (f"They copied the same exact {copied} as the original token in order to rank "
                 "higher on Coinbase search results than the real token, so that they could "
                 "deceive buyers into buying an impersonator token. They have already rugpulled, "
                 f"and the token is now at a {money(mcap)} market cap. It is extremely obvious "
                 "what they did.")
    else:
        cap = f" They are already at a {money(mcap)} market cap." if known_mcap(mcap) else ""
        story = (f"They are using the same exact {copied} as the original token in order to rank "
                 "higher on Coinbase search results than the real token, so that they can deceive "
                 f"buyers into buying an impersonator token and then rugpull them.{cap} It is "
                 "extremely obvious what they are doing.")

    # Every report asks 4 times to flag it as MALICIOUS, and twice to get it off Coinbase search.
    hurry = " Every hour it stays up on Coinbase, more people risk getting scammed." if second else ""
    asks = (
        "Please flag this CA as MALICIOUS.\n\n"
        "Flagging it as MALICIOUS is the only way that it actually shows a warning on Coinbase, "
        "which is where most people are currently getting scammed, so please flag it as MALICIOUS "
        f"and get it off of Coinbase search.{hurry}\n\n"
        f"Please double check and make sure that the MALICIOUS CA ( {ca} ) is flagged as MALICIOUS.\n\n"
        f"Once again, please flag {ca} as MALICIOUS and remove it from Coinbase search so that "
        "nobody else buys it thinking it is the original. Thank you for your help."
    )

    launched = "" if second else f" (launched {age_short(t.get('created_ms'))})"
    if solana:
        return (f"{opening}\n\n"
                f"Malicious CA{launched} : {ca}\n\n"
                f"Original CA (launched {REAL_TOKEN_AGE}) : {real}\n\n"
                f"{story}\n\n{asks}")
    return (f"{opening}\n\n"
            f"MALICIOUS CA{launched} - {ca}\n\n"
            f"{story}\n\n"
            f"For reference, here is the ORIGINAL CA deployed {REAL_TOKEN_AGE} - {real}\n\n"
            f"{asks}")


def send_report_alerts(t):
    chain = "Solana" if str(t.get("chain")).lower() == "solana" else "Base"
    notify(
        f"{chain} $IOF bonded - report it",
        report_text(t),
        click=REPORT_URL, priority=4,
        actions=[
            {"action": "view", "label": "Open report form", "url": REPORT_URL},
            {"action": "view", "label": "View token", "url": t.get("url") or REPORT_URL},
        ],
    )


# ---- Email to Blockaid -----------------------------------------------------

def explorer_link(t):
    chain = str(t.get("chain", "")).lower()
    ca = t.get("address")
    if chain == "base":
        return f"https://basescan.org/token/{ca}"
    if chain == "solana":
        return f"https://solscan.io/token/{ca}"
    return t.get("url") or ""


def send_email(to, subject, body, reply_to_id=None):
    """Sends one email. Returns its Message-ID (so a follow-up can reply in
    the same thread), or None if it failed."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("GMAIL_ADDRESS or GMAIL_APP_PASSWORD secret is missing, so no email was sent.")
        return None
    msg = EmailMessage()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = to
    msg["Subject"] = subject
    msg_id = make_msgid(domain="gmail.com")
    msg["Message-ID"] = msg_id
    if reply_to_id:
        msg["In-Reply-To"] = reply_to_id
        msg["References"] = reply_to_id
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        print(f"Email sent to {to}: {subject}")
        return msg_id
    except Exception as e:
        print(f"Email failed to send ({subject}): {e}")
        return None


def email_body(t, mcap=None, second=False):
    return (report_text(t, mcap=mcap, second=second)
            + f"\n\nBlock explorer link for the MALICIOUS token: {explorer_link(t)}"
            + (f"\nChart: {t['url']}" if t.get("url") else ""))


def email_subject(t):
    chain = "Solana" if str(t.get("chain")).lower() == "solana" else "Base"
    return f"MALICIOUS token report ({chain}): {t['address']}"


def under_daily_limit(state, ca):
    today = datetime.date.today().isoformat()
    counter = state.setdefault("email_count", {})
    if counter.get("date") != today:
        counter.clear()
        counter.update({"date": today, "count": 0})
    if counter["count"] >= DAILY_EMAIL_LIMIT:
        print(f"Daily email limit ({DAILY_EMAIL_LIMIT}) reached; not emailing {ca}.")
        notify("Daily Blockaid email limit reached",
               f"Not auto-emailed (report it manually if needed):\n\n{ca}",
               click=REPORT_URL, priority=3)
        return False
    return True


def email_blockaid(t, state):
    """First report for a new fake, with safety checks."""
    ca = t.get("address")
    if not ca or ca == "-" or is_legit(ca):
        return
    if str(t.get("chain")).lower() not in ALERT_CHAINS:
        return
    if EMAIL_ONLY_CLOSE_COPIES and not (t.get("exact_ticker") or t.get("name_match")):
        print(f"Not auto-emailing {t.get('name')} (${t.get('symbol')}): loose match, phone alert only.")
        return
    emailed = state.setdefault("emailed", [])
    if same_ca(ca) in {same_ca(e) for e in emailed}:
        print(f"Already emailed {ca} before; skipping.")
        return
    if not under_daily_limit(state, ca):
        return
    subject = email_subject(t)
    msg_id = send_email(BLOCKAID_EMAIL, subject, email_body(t))
    if msg_id:
        emailed.append(ca)
        state["email_count"]["count"] += 1
        state.setdefault("reports", {})[same_ca(ca)] = {
            "address": ca, "chain": t.get("chain"), "name": t.get("name"),
            "symbol": t.get("symbol"), "exact_ticker": t.get("exact_ticker"),
            "name_match": t.get("name_match"), "url": t.get("url"),
            "subject": subject, "message_id": msg_id,
            "sent_ms": time.time() * 1000, "followed_up": False,
        }


def follow_ups(state):
    """One follow-up per fake, 1 hour after its first report, only if it's still up."""
    if not FOLLOW_UP:
        return
    now = time.time() * 1000
    for key, r in state.setdefault("reports", {}).items():
        if r.get("followed_up"):
            continue  # already done (or skipped) - never again
        waited = now - r.get("sent_ms", now)
        if waited < FOLLOW_UP_AFTER_MINUTES * 60000:
            continue  # not an hour yet
        if waited > FOLLOW_UP_GIVE_UP_HOURS * 3600000:
            r["followed_up"] = "too old"
            continue
        res = live_pairs(r["address"])
        if res is None:
            continue  # lookup failed; try again next run
        bonded, _ = res
        if not bonded:
            r["followed_up"] = "gone"  # no longer trading, nothing to follow up on
            print(f"{r['address']} is no longer up; no follow-up needed.")
            continue
        if not under_daily_limit(state, r["address"]):
            continue
        best = max(bonded, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        mcap = best.get("marketCap") or best.get("fdv")
        r["followed_up"] = True  # mark first, so it can never send twice
        msg_id = send_email(BLOCKAID_EMAIL, "Re: " + r["subject"],
                            email_body(r, mcap=mcap, second=True),
                            reply_to_id=r.get("message_id"))
        if msg_id:
            state["email_count"]["count"] += 1
            notify("Follow-up report sent",
                   f"{r.get('name')} (${r.get('symbol')}) was still up 1 hour after the first "
                   f"report, so a second report went to Blockaid in the same thread.\n\n{r['address']}",
                   priority=3)


def send_test_email():
    """Sends a sample first report AND a sample follow-up to YOUR OWN Gmail
    (never to Blockaid), threaded together like the real ones."""
    sample = {
        "name": "TEST Institutional Oil Fund", "symbol": "IOF", "chain": "solana",
        "address": "TEST1111111111111111111111111111111111pump",
        "url": None, "mcap": 1_000_000,
        "created_ms": time.time() * 1000 - 5 * 60 * 1000,
        "exact_ticker": True, "name_match": True,
    }
    subject = "[TEST - not sent to Blockaid] " + email_subject(sample)
    first = send_email(GMAIL_ADDRESS, subject, email_body(sample))
    if first:
        send_email(GMAIL_ADDRESS, "Re: " + subject,
                   email_body(sample, mcap=30_000, second=True), reply_to_id=first)


# ---- Main ------------------------------------------------------------------

def main():
    if os.environ.get("SEND_TEST_EMAIL", "").lower() == "true":
        send_test_email()

    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    if state.get("mode") != MODE:
        state = {"mode": MODE, "initialized": False, "seen": {}}

    current = {}
    for checker in (check_dexscreener, check_geckoterminal, check_new_pools,
                    check_pumpfun, check_coinbase_official):
        try:
            results = checker()
            print(f"{checker.__name__}: {len(results)} matches")
            for k, v in results.items():
                k = norm_key(k)
                if k not in current:
                    current[k] = v
                    continue
                cur = current[k]
                for field in ("created_ms", "bonded_ms"):  # keep the earliest times
                    if v.get(field) and (not cur.get(field) or v[field] < cur[field]):
                        cur[field] = v[field]
                for field in ("mcap", "liquidity_usd", "url"):  # fill in anything missing
                    if not cur.get(field) and v.get(field):
                        cur[field] = v[field]
        except Exception as e:
            print(f"{checker.__name__} failed: {e}")

    for v in current.values():
        nm = str(v.get("name") or "")
        if len(nm) > 60:
            v["name"] = nm[:57] + "..."

    seen_keys = {norm_key(s) for s in state["seen"]}
    new_keys = [k for k in current if k not in seen_keys]

    # Coins found only on pump.fun: look up when they bonded to PumpSwap.
    fill_bond_times(current, [k for k in new_keys if not current[k].get("bonded_ms")])

    now_ms = time.time() * 1000
    window_ms = BOND_WINDOW_MINUTES * 60 * 1000
    to_alert, quiet, waiting = [], [], []
    for k in new_keys:
        t = current[k]
        bonded = t.get("bonded_ms")
        if not bonded:
            waiting.append(k)
            continue
        fresh = now_ms - bonded <= window_ms
        if (state.get("initialized") and fresh and t.get("reportable")
                and str(t.get("chain")).lower() in ALERT_CHAINS):
            to_alert.append(k)
        else:
            quiet.append(k)
    print(f"Matches found now: {len(current)}, new: {len(new_keys)}, "
          f"just bonded (pinging): {len(to_alert)}, older (filed quietly): {len(quiet)}, "
          f"waiting: {len(waiting)}")
    state["initialized"] = True
    state["sources_v2"] = True

    for k in to_alert + quiet:
        state["seen"][k] = current[k]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)

    for k in to_alert:
        send_report_alerts(current[k])
        if EMAIL_REPORTS:
            email_blockaid(current[k], state)

    if EMAIL_REPORTS:
        follow_ups(state)

    state["heartbeat"] = datetime.date.today().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
