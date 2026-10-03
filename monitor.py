"""
IOF / Institutional Oil Fund token monitor.

Checks two places every run:
  1. DexScreener  - onchain tokens (Base, Solana, Ethereum, etc.), which is
                    what shows up in the Coinbase app's search bar.
  2. Coinbase     - Coinbase's official exchange listings.

Sends a phone notification (via the free ntfy app) whenever a token
matching the name or ticker below has BONDED (graduated off its launchpad's
bonding curve onto a real exchange) and hasn't been alerted on before.
A token still on its bonding curve is ignored until it bonds.

For each new fake, it also sends a ready-to-paste scam report plus a button
that opens the Blockaid report form. The real token below is never alerted
on or reported.
"""

import datetime
import json
import os
import urllib.parse
import urllib.request

# ---- What to look for (you can edit these) ---------------------------------
NAME_MATCH = "institutional oil fund"  # matched anywhere in the name, any case
SYMBOL_MATCH = "IOF"                    # exact ticker match
SEARCH_TERMS = ["IOF", "Institutional Oil Fund"]

# DexScreener labels for launchpad bonding curves. A token only counts as
# bonded once it trades somewhere NOT on this list, with real liquidity.
BONDING_CURVE_DEXES = {"pumpfun", "moonshot", "launchlab", "boop", "believe",
                       "bonk", "letsbonk", "heaven", "jupstudio", "dbc"}

# The REAL token. It is never alerted on and never gets a report drafted.
LEGIT_CAS = {"2sY7rkMCQyFNcSpHm3fciJf2ptYRg3cYpn4srN6Bpump"}

REPORT_URL = "https://report.blockaid.io/scam"
# ----------------------------------------------------------------------------

STATE_FILE = "seen.json"
MODE = "bonded-v1"
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()


def get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "iof-monitor/1.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def matches(name, symbol):
    name = (name or "").lower()
    symbol = (symbol or "").upper().lstrip("$").strip()
    return symbol == SYMBOL_MATCH or NAME_MATCH in name


def is_bonded_pair(p):
    dex = (p.get("dexId") or "").lower()
    liq = (p.get("liquidity") or {}).get("usd") or 0
    return dex not in BONDING_CURVE_DEXES and liq > 0


def check_dexscreener():
    tokens = {}
    for term in SEARCH_TERMS:
        url = "https://api.dexscreener.com/latest/dex/search?q=" + urllib.parse.quote(term)
        data = get_json(url)
        for p in data.get("pairs") or []:
            bt = p.get("baseToken") or {}
            if not matches(bt.get("name"), bt.get("symbol")):
                continue
            if bt.get("address") in LEGIT_CAS:
                continue  # never touch the real token
            key = f"dex:{p.get('chainId', '?')}:{bt.get('address', '?')}"
            tokens.setdefault(key, {"info": bt, "pairs": {}})
            tokens[key]["pairs"][p.get("pairAddress") or p.get("url")] = p

    found = {}
    for key, t in tokens.items():
        bonded = [p for p in t["pairs"].values() if is_bonded_pair(p)]
        if not bonded:
            continue  # still on its bonding curve; check again next run
        best = max(bonded, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        created = [p.get("pairCreatedAt") for p in t["pairs"].values() if p.get("pairCreatedAt")]
        found[key] = {
            "source": f"Onchain - bonded, trading on {best.get('dexId')}",
            "name": t["info"].get("name"),
            "symbol": t["info"].get("symbol"),
            "chain": best.get("chainId", "?"),
            "address": t["info"].get("address", "?"),
            "url": best.get("url"),
            "liquidity_usd": (best.get("liquidity") or {}).get("usd"),
            "created_ms": min(created) if created else None,
            "reportable": True,
        }
    return found


def check_coinbase_official():
    found = {}
    for c in get_json("https://api.exchange.coinbase.com/currencies"):
        if matches(c.get("name"), c.get("id")):
            found[f"coinbase:{c.get('id')}"] = {
                "source": "Coinbase official listing",
                "name": c.get("name"),
                "symbol": c.get("id"),
                "chain": "Coinbase Exchange",
                "address": "-",
                "url": f"https://www.coinbase.com/price/{str(c.get('id')).lower()}",
                "liquidity_usd": None,
            }
    return found


def notify(title, message, click=None, priority=3, actions=None):
    if not TOPIC:
        print("NTFY_TOPIC secret is missing, so no notification was sent.")
        print(title, "-", message)
        return
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
    urllib.request.urlopen(req, timeout=30).read()


def describe(t):
    liq = t.get("liquidity_usd")
    liq_text = f"${liq:,.0f}" if isinstance(liq, (int, float)) else "unknown"
    return (f"{t['name']} (${t['symbol']})\n"
            f"Where: {t['source']} - {t['chain']}\n"
            f"Contract: {t['address']}\n"
            f"Liquidity: {liq_text}")


def age_text(created_ms):
    if not created_ms:
        return "recently"
    now = datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000
    minutes = max(0, int((now - created_ms) / 60000))
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"approximately {days} day{'s' if days != 1 else ''} and {hours} hour{'s' if hours != 1 else ''} ago"
    if hours:
        return f"approximately {hours} hour{'s' if hours != 1 else ''} and {mins} minute{'s' if mins != 1 else ''} ago"
    return f"approximately {mins} minute{'s' if mins != 1 else ''} ago"


def report_text(t):
    ca = t["address"]
    real = next(iter(LEGIT_CAS))
    return (
        "I am writing to report a scam token that is impersonating an existing project.\n\n"
        f"Impersonator (scam) token CA:\n{ca}\n\n"
        f"Original (legitimate) token CA:\n{real}\n\n"
        f"The impersonator token was deployed {age_text(t.get('created_ms'))}. "
        "It is a direct clone of the original Institutional Oil Fund ($IOF) token, using the "
        "same name, ticker, and profile image. The original token was deployed almost two weeks ago.\n\n"
        "The deployer of the impersonator token bundled over 95% of the supply and appears to be "
        "using bots to create fake holders and inflate trading volume. The apparent goal is to rank "
        "higher in search results on platforms such as Coinbase, deceive buyers into purchasing the "
        "counterfeit token, and then execute a rug pull.\n\n"
        "I respectfully request that you investigate and flag this token as soon as possible to "
        "protect users from losing their funds.\n\n"
        f"Scam token CA (for reference):\n{ca}\n\n"
        "Thank you for your time and attention to this matter."
    )


def send_report_alerts(t):
    chain = str(t.get("chain", "")).capitalize()
    notify(
        "Fake $IOF bonded - report it",
        f"{t['name']} (${t['symbol']}) on {chain}\n"
        f"CA: {t['address']}\n\n"
        "Form: Scam > Domain coinbase.com, Address = the scam CA above, "
        f"Chain {chain}, Wallet Coinbase, Txn hash empty, your email.\n"
        "Check the holders first: only send if the 95% bundle line is true for this one. "
        "The next alert is the report text to copy.",
        click=REPORT_URL, priority=4,
        actions=[
            {"action": "view", "label": "Open report form", "url": REPORT_URL},
            {"action": "view", "label": "View token", "url": t.get("url") or REPORT_URL},
        ],
    )
    notify("Report text (copy this)", report_text(t), priority=3)


def main():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    if state.get("mode") != MODE:  # first run, or upgraded from the old version
        state = {"mode": MODE, "initialized": False, "seen": {}}

    current = {}
    for checker in (check_dexscreener, check_coinbase_official):
        try:
            current.update(checker())
        except Exception as e:  # one source failing shouldn't stop the other
            print(f"{checker.__name__} failed: {e}")

    new_keys = [k for k in current if k not in state["seen"]]
    print(f"Matches found now: {len(current)}, new: {len(new_keys)}")

    if not state.get("initialized"):
        names = ", ".join(sorted({f"{t['name']} on {t['chain']}" for t in current.values()})[:5])
        notify("IOF monitor is on (bonded only)",
               f"Watching for newly bonded tokens. Already bonded matches: {len(current)}."
               + (f"\nExamples: {names}" if names else ""))
        state["initialized"] = True
    else:
        for k in new_keys[:5]:
            t = current[k]
            if t.get("reportable"):
                send_report_alerts(t)
            else:
                notify(f"{SYMBOL_MATCH} token just bonded", describe(t), click=t.get("url"), priority=4)
        if len(new_keys) > 5:
            notify(f"{len(new_keys) - 5} more bonded {SYMBOL_MATCH} tokens",
                   "Check DexScreener for the rest.")

    for k in new_keys:
        state["seen"][k] = current[k]
    # Changes once a day so GitHub sees activity and keeps the schedule running.
    state["heartbeat"] = datetime.date.today().isoformat()

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
