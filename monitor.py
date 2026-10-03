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
            key = f"dex:{p.get('chainId', '?')}:{bt.get('address', '?')}"
            tokens.setdefault(key, {"info": bt, "pairs": {}})
            tokens[key]["pairs"][p.get("pairAddress") or p.get("url")] = p

    found = {}
    for key, t in tokens.items():
        bonded = [p for p in t["pairs"].values() if is_bonded_pair(p)]
        if not bonded:
            continue  # still on its bonding curve; check again next run
        best = max(bonded, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        found[key] = {
            "source": f"Onchain - bonded, trading on {best.get('dexId')}",
            "name": t["info"].get("name"),
            "symbol": t["info"].get("symbol"),
            "chain": best.get("chainId", "?"),
            "address": t["info"].get("address", "?"),
            "url": best.get("url"),
            "liquidity_usd": (best.get("liquidity") or {}).get("usd"),
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


def notify(title, message, click=None, priority=3):
    if not TOPIC:
        print("NTFY_TOPIC secret is missing, so no notification was sent.")
        print(title, "-", message)
        return
    payload = {"topic": TOPIC, "title": title, "message": message,
               "priority": priority, "tags": ["oil_drum"]}
    if click:
        payload["click"] = click
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
