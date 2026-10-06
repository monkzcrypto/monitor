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
import re
import json
import os
import urllib.parse
import urllib.request

# ---- What to look for (you can edit these) ---------------------------------
SYMBOL_MATCH = "IOF"                    # any ticker or name CONTAINING this counts
NAME_MATCH = "institutional oil fund"   # any name containing this (or close spellings) counts
# Double ping = ticker is exactly IOF AND the name is Institutional Oil Fund.
SEARCH_TERMS = ["IOF", "$IOF", "Institutional Oil Fund", "IOF pump", "IOF solana"]

# DexScreener labels for launchpad bonding curves. A token only counts as
# bonded once it trades somewhere NOT on this list, with real liquidity.
BONDING_CURVE_DEXES = {"pumpfun", "moonshot", "launchlab", "boop", "believe",
                       "bonk", "letsbonk", "heaven", "jupstudio", "dbc"}

# The REAL token. It is never alerted on and never gets a report drafted.
LEGIT_CAS = {"2sY7rkMCQyFNcSpHm3fciJf2ptYRg3cYpn4srN6Bpump"}

REPORT_URL = "https://report.blockaid.io/scam"
# ----------------------------------------------------------------------------

STATE_FILE = "seen.json"
MODE = "bonded-v4"
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()


def get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "iof-monitor/1.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def clean_symbol(symbol):
    return (symbol or "").upper().lstrip("$").strip()


def name_matches(name):
    """Institutional Oil Fund, including spacing tricks and close misspellings."""
    letters = re.sub(r"[^a-z]", "", (name or "").lower())
    target = re.sub(r"[^a-z]", "", NAME_MATCH)
    return target in letters or ("instit" in letters and "oilfund" in letters)


def exact_ticker(symbol):
    return letters_only(symbol) == SYMBOL_MATCH.lower()


def letters_only(text):
    return re.sub(r"[^a-z]", "", (text or "").lower())


def matches(name, symbol):
    """Counts if IOF appears anywhere in the ticker or name (also "I O F", "I.O.F"),
    or the name is Institutional Oil Fund."""
    key = SYMBOL_MATCH.lower()
    return (key in letters_only(symbol)
            or key in letters_only(name)
            or name_matches(name))


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
            "mcap": best.get("marketCap") or best.get("fdv"),
            "name_match": name_matches(t["info"].get("name")),
            "exact_ticker": exact_ticker(t["info"].get("symbol")),
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


def age_short(created_ms):
    """e.g. '5 minutes ago (3:42 PM ET, Oct 6)'"""
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


def mcap_text(mcap):
    if not isinstance(mcap, (int, float)) or mcap <= 0:
        return "an inflated"
    for size, label in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if mcap >= size:
            return f"a ${mcap / size:,.1f}{label}".replace(".0" + label, label)
    return f"a ${mcap:,.0f}"


def copied_what(t):
    if t.get("exact_ticker") and t.get("name_match"):
        return "ticker, name, and pfp"
    if t.get("exact_ticker"):
        return "ticker and pfp"
    return f"branding (they launched as \"{t.get('name')}\" with the ticker ${t.get('symbol')})"


def report_text(t):
    ca = t["address"]
    real = next(iter(LEGIT_CAS))
    age = age_short(t.get("created_ms"))
    mcap = mcap_text(t.get("mcap"))
    chain = str(t.get("chain", "")).lower()

    if chain == "solana":
        same = ("same exact ticker and PFP" if t.get("exact_ticker")
                else "same branding (" + copied_what(t).split("(", 1)[-1])
        return (
            "I am writing to report the token with the following CA as a MALICIOUS TOKEN. "
            f"They bundled to {mcap} market cap, and are wash trading and spoofing their holder "
            "count in order to rank higher on Coinbase search results than the real token so that "
            "they can deceive buyers into buying an impersonator token and rugpull them. "
            f"They are using the {same} as the original token that was deployed over 2 weeks ago "
            "and it is extremely obvious what they are doing. I urge you to look into this and flag "
            "them as a MALICIOUS token so that it is reflected in the search of platforms like "
            "Coinbase and FOMO and people stop getting scammed.\n\n"
            f"Malicious CA (launched {age}) : {ca}\n\n"
            f"Original CA (launched over 2 weeks ago) : {real}\n\n"
            f"And please double check and make sure that the MALICIOUS CA ( {ca} ) is classified "
            "as MALICIOUS as that is the only way that it actually shows a warning on Coinbase, "
            "which is where most people are currently getting scammed. Thank you for your help."
        )

    chain_name = "BASE" if chain == "base" else chain.upper()
    return (
        f"I am writing to report the following CA on {chain_name} chain as a MALICIOUS token -\n\n"
        f"{ca}\n\n"
        f"They deployed {age}, and are already at {mcap} market cap, directly copying the "
        f"{copied_what(t)} of the original token that was deployed over 2 weeks ago. They do this in "
        "order to rank higher on platforms' search, for example Coinbase. They wait for people to "
        "buy their token thinking it is the original and then they rugpull. Please flag this CA as "
        "MALICIOUS as that is the only flag that will remove it from Coinbase search and keep "
        "users safe.\n\n"
        "For reference, here is the ORIGINAL CA deployed over 2 weeks ago -\n\n"
        f"{real}\n\n"
        "Once again, the MALICIOUS CA is -\n\n"
        f"{ca}\n\n"
        "Please flag it as MALICIOUS so that people don't get scammed. Thank you"
    )


def send_report_alerts(t):
    chain = str(t.get("chain", "")).capitalize()
    exact = t.get("exact_ticker")
    if t.get("name_match") and exact:
        notify("SAME NAME + TICKER - full clone", f"{t['name']} (${t['symbol']}) copies the name AND ticker.\nCA: {t['address']}", priority=5)
    notify(
        "Fake $IOF bonded - report it" if exact else "IOF-related token bonded - check it",
        f"{t['name']} (${t['symbol']}) on {chain}\n"
        f"CA: {t['address']}\n\n"
        "Form: Scam > Domain coinbase.com, Address = the scam CA above, "
        f"Chain {chain}, Wallet Coinbase, Txn hash empty, your email.\n"
        + ("" if exact else "This one only CONTAINS IOF, so make sure it's really posing as $IOF before reporting. ")
        + "Quick check before sending: make sure the bundling/copying claims fit this one. "
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

