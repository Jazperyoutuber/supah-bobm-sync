"""
Supah Bobm Survival supporter sync.

Reads who has the Iron / Nitro / Rainbow Rank roles in the Discord (Patreon hands those roles out by itself), and the
VRChat name each of them posted in the #vrchat-link channel, and writes supporters.txt: one "rank<TAB>VRChat name" a
line, which the world downloads (BS_Skins). Only Python's own library is used, so GitHub's runner needs nothing installed.

Every patron's latest message in the channel is their name. The bot reacts to it: a tick once it's on the list, a
question mark while that person has no rank role (not a patron yet, or Patreon hasn't linked their Discord).

Settings are in config.json; the bot's token comes from the DISCORD_TOKEN environment variable (a GitHub secret).
If Discord can't be read, supporters.txt is left as it was and the script exits with an error.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://discord.com/api/v10"
HERE = os.path.dirname(os.path.abspath(__file__))
RANK_WORDS = {3: "rainbow", 2: "nitro", 1: "iron"}
TICK = "%E2%9C%85"        # the check mark emoji, URL-encoded
QUESTION = "%E2%9D%93"    # the red question mark
MAX_NAME = 64


class DiscordError(Exception):
    pass


def call(method, path, token, body=None):
    """One Discord API request, waiting out rate limits. Returns the decoded JSON (or None)."""
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(6):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bot " + token)
        req.add_header("User-Agent", "DiscordBot (https://github.com, 1.0) SupahBobmSupporterSync")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code == 429:
                try:
                    wait = float(json.loads(text).get("retry_after", 1.0))
                except ValueError:
                    wait = 1.0
                time.sleep(min(wait, 30.0) + 0.2)
                continue
            if e.code >= 500 and attempt < 5:
                time.sleep(2 + attempt * 2)
                continue
            raise DiscordError("%s %s -> HTTP %d: %s" % (method, path, e.code, text[:300]))
        except urllib.error.URLError as e:
            if attempt < 5:
                time.sleep(2 + attempt * 2)
                continue
            raise DiscordError("%s %s -> %s" % (method, path, e))
    raise DiscordError("%s %s -> still rate limited" % (method, path))


def clean_name(content):
    """The VRChat name in a message: its first line, without Discord formatting, mentions or a 'VRChat:' label."""
    if not content:
        return ""
    line = content.strip().splitlines()[0].strip()
    line = re.sub(r"<[@#:!&a-zA-Z0-9_]+>", "", line)              # mentions, channels, custom emoji
    line = line.replace("`", "").replace("**", "").replace("__", "").replace("||", "").replace("~~", "")
    line = re.sub(r"^(my\s+)?(vrchat|vrc)(\s+name)?(\s+is)?\s*[:\-=]?\s*", "", line, flags=re.IGNORECASE)
    line = line.strip().strip('"').strip("'").strip()
    # Tabs and line breaks would break the list's format.
    line = re.sub(r"\s+", " ", line)
    return line[:MAX_NAME]


def best_rank(member_roles, rank_of_role):
    best = 0
    for rid in member_roles:
        best = max(best, rank_of_role.get(rid, 0))
    return best


def read_all(token, cfg):
    guild = str(cfg["guild_id"]).strip()
    channel = str(cfg["channel_id"]).strip()
    if not guild.isdigit() or not channel.isdigit():
        raise DiscordError("config.json needs your server's guild_id and the link channel's channel_id (numbers, see the README).")

    # Which roles mean which rank: by name (any case) or by id.
    roles = call("GET", "/guilds/%s/roles" % guild, token) or []
    rank_of_role = {}
    for rank_key, rank in (("rainbow", 3), ("nitro", 2), ("iron", 1)):
        wanted = [str(x).strip().lower() for x in cfg.get("rank_roles", {}).get(rank_key, [])]
        for role in roles:
            if role["id"] in wanted or role["name"].strip().lower() in wanted:
                rank_of_role[role["id"]] = max(rank_of_role.get(role["id"], 0), rank)
    for rank in (3, 2, 1):
        if rank not in rank_of_role.values():
            print("warning: no Discord role found for the %s rank - check rank_roles in config.json" % RANK_WORDS[rank])

    # Everyone in the server and their best rank (needs the Server Members intent).
    ranks = {}
    after = "0"
    while True:
        page = call("GET", "/guilds/%s/members?limit=1000&after=%s" % (guild, after), token) or []
        for m in page:
            r = best_rank(m.get("roles", []), rank_of_role)
            if r > 0:
                ranks[m["user"]["id"]] = r
        if len(page) < 1000:
            break
        after = page[-1]["user"]["id"]

    # Each person's latest message in the link channel (needs the Message Content intent to see what it says).
    latest = {}
    before = None
    seen_messages = 0
    limit = int(cfg.get("max_messages", 5000))
    empty_contents = 0
    while seen_messages < limit:
        path = "/channels/%s/messages?limit=100" % channel + ("&before=%s" % before if before else "")
        page = call("GET", path, token) or []
        if not page:
            break
        for msg in page:   # newest first
            seen_messages += 1
            author = msg.get("author", {})
            if author.get("bot"):
                continue
            uid = author.get("id")
            if uid in latest:
                continue
            if not msg.get("content"):
                empty_contents += 1
            latest[uid] = msg
        before = page[-1]["id"]
        if len(page) < 100:
            break
    if latest and empty_contents == len(latest):
        print("warning: every message came back empty - turn on the bot's Message Content intent (see the README).")
    return channel, ranks, latest


def react(token, channel, msg, has_rank):
    """A tick for a name on the list, a question mark for one without a rank role (swapping them as that changes)."""
    mine = {r["emoji"].get("name") for r in msg.get("reactions", []) if r.get("me")}
    want, other = (TICK, QUESTION) if has_rank else (QUESTION, TICK)
    want_char = "✅" if has_rank else "❓"
    other_char = "❓" if has_rank else "✅"
    try:
        if other_char in mine:
            call("DELETE", "/channels/%s/messages/%s/reactions/%s/@me" % (channel, msg["id"], other), token)
        if want_char not in mine:
            call("PUT", "/channels/%s/messages/%s/reactions/%s/@me" % (channel, msg["id"], want), token)
    except DiscordError as e:
        # Not being able to react (no Add Reactions permission) doesn't stop the list.
        print("note: couldn't react to a message: %s" % e)


def main():
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        print("error: DISCORD_TOKEN isn't set (add it as a repository secret, see the README).")
        return 2
    try:
        channel, ranks, latest = read_all(token, cfg)
    except DiscordError as e:
        print("error: %s" % e)
        return 2

    entries = {}
    for uid, msg in latest.items():
        name = clean_name(msg.get("content", ""))
        r = ranks.get(uid, 0)
        if cfg.get("react", True):
            react(token, channel, msg, r > 0 and bool(name))
        if r > 0 and name:
            key = name.lower()
            # Two people claiming one name: the higher rank keeps it.
            if key not in entries or entries[key][0] < r:
                entries[key] = (r, name)

    rows = sorted(entries.values(), key=lambda e: (-e[0], e[1].lower()))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Supah Bobm Survival supporters - written by sync.py, don't edit (use extras.txt for hand-added names).",
        "# rank<TAB>VRChat name. Updated %s: %d supporters (%d rainbow, %d nitro, %d iron)." % (
            stamp, len(rows), sum(1 for r in rows if r[0] == 3), sum(1 for r in rows if r[0] == 2), sum(1 for r in rows if r[0] == 1)),
    ]
    lines += ["%s\t%s" % (RANK_WORDS[r], name) for r, name in rows]
    new_body = "\n".join(lines[2:])
    path = os.path.join(HERE, "supporters.txt")
    old_body = ""
    if os.path.exists(path):
        old_lines = open(path, encoding="utf-8").read().splitlines()
        old_body = "\n".join(l for l in old_lines if not l.startswith("#"))
    # Only rewritten when someone was added, removed or changed rank (the date alone isn't worth a commit).
    if new_body != old_body or not os.path.exists(path):
        open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
        print("supporters.txt updated: %d supporters" % len(rows))
    else:
        print("no change: %d supporters" % len(rows))

    # A monthly heartbeat commit, so GitHub doesn't switch the 30-minute schedule off after 60 quiet days.
    beat = os.path.join(HERE, "heartbeat.txt")
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    if not os.path.exists(beat) or open(beat, encoding="utf-8").read().strip() != month:
        open(beat, "w", encoding="utf-8", newline="\n").write(month + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
