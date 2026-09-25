#!/usr/bin/env python3
"""Origins TCG match tracker.

The game writes exactly one replay file per match to its data dir and
overwrites it on the next match, so this tool copies every new replay into
data/replays/, decodes it, and keeps a SQLite history from which it computes
win rate and per-card stats.

Commands:
  python3 tracker.py import          # ingest whatever replay is in the game dir right now
  python3 tracker.py watch           # keep running; ingest each new replay as it appears
  python3 tracker.py label W|L [id]  # mark the latest (or given) match as a win or loss
  python3 tracker.py stats [build]   # print win rate and card stats (optionally for Demo / Playtest only)
  python3 tracker.py serve [port]    # local dashboard (default http://localhost:8787)
"""
import errno
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from replay_format import parse

HERE = os.path.dirname(os.path.abspath(__file__))


def find_game_dirs():
    """Unity persistentDataPath folders for the game, one per installed build.

    On macOS every build (demo, playtest, release) shares one folder keyed on the
    bundle id. On Windows each build gets its own LocalLow folder keyed on the
    product name, so all that exist are watched. Override with ORIGINS_GAME_DIR
    (one path, or several separated by the OS path separator).
    """
    env = os.environ.get("ORIGINS_GAME_DIR")
    if env:
        return env.split(os.pathsep)
    if sys.platform == "darwin":
        candidates = [os.path.expanduser("~/Library/Application Support/io.koingames.originstcg.game")]
    elif sys.platform.startswith("win"):
        low = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "AppData", "LocalLow", "Koin Games")
        candidates = [os.path.join(low, n) for n in ("Origins TCG Demo", "Origins TCG Playtest", "Origins TCG")]
    else:
        base = os.path.expanduser("~/.config/unity3d/Koin Games")
        candidates = [os.path.join(base, n) for n in ("Origins TCG Demo", "Origins TCG Playtest", "Origins TCG")]
    found = [c for c in candidates if os.path.isdir(c)]
    return found or candidates[:1]


GAME_DIRS = find_game_dirs()
GAME_DIR = GAME_DIRS[0]  # kept for messages; every lookup below scans GAME_DIRS
DATA_DIR = os.path.join(HERE, "data")
REPLAY_DIR = os.path.join(DATA_DIR, "replays")
DB_PATH = os.path.join(DATA_DIR, "tracker.db")

# Event/field ids observed in build 0.6.3 replays. See replay_format.py for the grammar.
EV_DRAW = 20        # card enters hand: 50 = card instance id
EV_DRAG = 40        # drag in progress/drop: 50 = {1: instance id}, 51 lane, 53 slot, 54 dropped
EV_UNDO = 41        # placement picked back up
EV_READY = 50       # player clicked ready
EV_PHASE = 51       # phase advanced (player 255 = system)
EV_EMOTE = 60       # 50 = emote key
COMMIT_PLACE = 3    # committed placement in the round summary: 50 instance id, 51 lane, 53 slot


# ----------------------------------------------------------------------------- card db
def load_card_db():
    """Card definitions the client downloaded (playtest and demo buckets)."""
    cards, locations = {}, {}
    def each(*parts):
        for g in GAME_DIRS:
            yield from glob.glob(os.path.join(g, *parts))
    for f in each("Data", "*", "*", "current", "CardBaseData", "GameData_CardBase_Data_*.json"):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        cards.setdefault(d["Key"], {k: d.get(k) for k in ("Name", "Type", "Rarity", "SubType", "ManaCost", "Power", "Health")})
    for f in each("Data", "*", "*", "current", "LocationData", "GameData_Locations_Data_*.json"):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        locations.setdefault(d["Key"], d.get("Name"))
    if not cards:  # fall back to the snapshot committed next to this script
        snap = os.path.join(HERE, "carddb_snapshot.json")
        if os.path.exists(snap):
            s = json.load(open(snap))
            cards, locations = s["cards"], {k: v.get("Name") for k, v in s["locations"].items()}
    return cards, locations


def base_key(variant_key):
    return variant_key.split("_V")[0]


def detect_build(replay_path):
    """Which build (Demo / Playtest / release) produced a replay.

    Windows keeps one data folder per build, so the folder name says. macOS shares
    one folder, but each build keeps its own Player.log under ~/Library/Logs, and
    the log of the build that was running is written right after the replay.
    """
    folder = os.path.basename(os.path.dirname(replay_path))
    if folder.startswith("Origins TCG"):
        return folder.replace("Origins TCG", "").strip() or "Release"
    if sys.platform == "darwin":
        t = os.path.getmtime(replay_path)
        best, best_dt = None, 30 * 60
        for log in glob.glob(os.path.expanduser("~/Library/Logs/Koin Games/*/Player.log")):
            dt = os.path.getmtime(log) - t
            if -60 <= dt < best_dt:
                best, best_dt = os.path.basename(os.path.dirname(log)), dt
        if best:
            return best.replace("Origins TCG", "").strip() or "Release"
    return None


# ----------------------------------------------------------------------------- decoding
def decode_replay(path):
    raw = open(path, "rb").read()
    d = parse(raw)
    m = re.match(r"LatestMatch_(.+)_vs_(.+)_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.replay$", os.path.basename(path))
    me_name, opp_name, stamp = (m.group(1), m.group(2), m.group(3)) if m else (None, None, None)
    played_at = datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S").isoformat(sep=" ") if stamp else None

    players = []
    for idx, p in enumerate(d[3]):
        deck = [c[0] for c in p[5][0] if not c[0].startswith("Tower")]
        players.append({
            "index": idx,
            "beamable_id": p[0],
            "name": p[1],
            "flag2": p.get(2),
            "flag4": p.get(4),
            "rank": p.get(7),
            "avatar": p.get(8),
            "commander": p.get(11),
            "deck": deck,
        })
    me = next((p for p in players if p["name"] == me_name), players[0])
    opp = next((p for p in players if p is not me), None)

    rounds = d[4]
    placements = []   # (round, player, instance id, lane, slot)
    for ri, r in enumerate(rounds):
        for e in r.get(0, []):
            if e.get(0) == COMMIT_PLACE:
                placements.append((ri, e.get(2), e.get(50), e.get(51), e.get(53)))
    # Mouse-cursor samples per player. A bot has no mouse, so zero samples over a
    # whole match marks the opponent as a bot.
    for p in players:
        p["cursor_points"] = sum(len(e.get(1, [])) for r in rounds for e in r.get(2, []) if e.get(0) == p["index"])
    conf = d[2]
    return {
        "file": os.path.basename(path),
        "played_at": played_at,
        "build": detect_build(path),
        "me": me, "opp": opp,
        "arena": conf.get(0), "seed": conf.get(1), "location_pool": conf.get(37),
        "header_flag": d.get(1),           # candidate result field, unverified
        "rounds": len(rounds),
        "placements": placements,
        "size": len(raw),
    }


# ----------------------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
  id TEXT PRIMARY KEY, played_at TEXT, me TEXT, opp TEXT,
  my_commander TEXT, opp_commander TEXT, my_rank TEXT, opp_rank TEXT,
  my_deck TEXT, opp_deck TEXT, arena TEXT, seed INTEGER, location_pool TEXT,
  rounds INTEGER, header_flag INTEGER, my_flag2 INTEGER, my_flag4 INTEGER,
  opp_flag2 INTEGER, opp_flag4 INTEGER, result TEXT, imported_at TEXT
);
CREATE TABLE IF NOT EXISTS placements (
  match_id TEXT, round INTEGER, player INTEGER, instance_id INTEGER, lane INTEGER, slot INTEGER
);
"""


def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(matches)")}
    for col, typ in (("build", "TEXT"), ("my_cursor_points", "INTEGER"), ("opp_cursor_points", "INTEGER")):
        if col not in cols:
            c.execute(f"ALTER TABLE matches ADD COLUMN {col} {typ}")
    return c


def ingest(path, conn=None):
    conn = conn or db()
    rec = decode_replay(path)
    me, opp = rec["me"], rec["opp"] or {}
    if conn.execute("SELECT 1 FROM matches WHERE id=?", (rec["file"],)).fetchone():
        # backfill columns added after this row was imported
        if rec["build"]:
            conn.execute("UPDATE matches SET build=? WHERE id=? AND build IS NULL", (rec["build"], rec["file"]))
        conn.execute("UPDATE matches SET my_cursor_points=?, opp_cursor_points=? WHERE id=? AND opp_cursor_points IS NULL",
                     (me["cursor_points"], opp.get("cursor_points"), rec["file"]))
        conn.commit()
        return None
    os.makedirs(REPLAY_DIR, exist_ok=True)
    dst = os.path.join(REPLAY_DIR, rec["file"])
    if not os.path.exists(dst):
        shutil.copy2(path, dst)
    conn.execute(
        "INSERT INTO matches (id, played_at, me, opp, my_commander, opp_commander, my_rank, opp_rank, my_deck, opp_deck, "
        "arena, seed, location_pool, rounds, header_flag, my_flag2, my_flag4, opp_flag2, opp_flag4, result, imported_at, build, "
        "my_cursor_points, opp_cursor_points) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec["file"], rec["played_at"], me["name"], opp.get("name"),
         me["commander"], opp.get("commander"), me["rank"], opp.get("rank"),
         json.dumps(me["deck"]), json.dumps(opp.get("deck", [])), rec["arena"], rec["seed"], rec["location_pool"],
         rec["rounds"], rec["header_flag"], me["flag2"], me["flag4"], opp.get("flag2"), opp.get("flag4"),
         None, datetime.now().isoformat(sep=" "), rec["build"], me["cursor_points"], opp.get("cursor_points")))
    conn.executemany("INSERT INTO placements VALUES (?,?,?,?,?,?)",
                     [(rec["file"], *p) for p in rec["placements"]])
    conn.commit()
    return rec


def game_replays():
    return sorted(p for g in GAME_DIRS for p in glob.glob(os.path.join(g, "LatestMatch_*.replay")))


def cmd_import():
    conn = db()
    n = 0
    for p in game_replays() + sorted(glob.glob(os.path.join(REPLAY_DIR, "*.replay"))):
        if ingest(p, conn):
            n += 1
            print("imported", os.path.basename(p))
    print(f"{n} new match(es)")


def cmd_watch(interval=2.0):
    conn = db()
    seen = set(os.path.basename(p) for p in game_replays())
    for p in game_replays():
        ingest(p, conn)
    print("watching " + ", ".join(GAME_DIRS) + " (ctrl-c to stop)")
    while True:
        for p in game_replays():
            name = os.path.basename(p)
            if name in seen:
                continue
            time.sleep(1.0)  # let the game finish writing
            rec = ingest(p, conn)
            seen.add(name)
            if rec:
                print(f"[{datetime.now():%H:%M:%S}] new match: {rec['me']['name']} vs {rec['opp']['name']} "
                      f"({rec['rounds']} rounds). Mark it: python3 tracker.py label W|L")
        time.sleep(interval)


def cmd_label(result, match_id=None):
    result = result.upper()[0]
    if result not in "WL":
        sys.exit("result must be W or L")
    conn = db()
    if not match_id:
        row = conn.execute("SELECT id FROM matches WHERE result IS NULL ORDER BY played_at DESC LIMIT 1").fetchone()
        if not row:
            sys.exit("no unlabeled match")
        match_id = row["id"]
    conn.execute("UPDATE matches SET result=? WHERE id=?", (result, match_id))
    conn.commit()
    print(f"{match_id}: {result}")


# ----------------------------------------------------------------------------- stats
def onboarding_record():
    """W/L string the client caches from Beamable for the onboarding bot matches."""
    for f in sorted(p for g in GAME_DIRS for p in glob.glob(os.path.join(g, "beamable", "cache", "*", "*", "*", "*.json"))):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        for r in d.get("results", []) if isinstance(d, dict) else []:
            for s in r.get("stats", []):
                if s.get("k") == "onboardingResults":
                    v = s["v"]
                    return {"wins": v.count("W"), "losses": v.count("L"), "sequence": v}
    return None


def compute_stats(conn, build=None):
    """Stats for all matches, or only those from one build (Demo / Playtest / ...)."""
    cards, _ = load_card_db()
    all_rows = conn.execute("SELECT * FROM matches ORDER BY played_at").fetchall()
    builds = sorted({r["build"] or "?" for r in all_rows})
    latest_build = (all_rows[-1]["build"] or "?") if all_rows else None
    rows = [r for r in all_rows if not build or (r["build"] or "?") == build]
    labeled = [r for r in rows if r["result"]]
    wins = sum(1 for r in labeled if r["result"] == "W")

    def name(key):
        c = cards.get(base_key(key or ""))
        return c["Name"] if c else key

    def group(keyfn):
        out = {}
        for r in labeled:
            k = keyfn(r)
            g = out.setdefault(k, {"games": 0, "wins": 0})
            g["games"] += 1
            g["wins"] += r["result"] == "W"
        return sorted(({"key": k, **v, "winrate": v["wins"] / v["games"]} for k, v in out.items()),
                      key=lambda g: (-g["games"], -g["winrate"]))

    card_stats = {}
    for r in labeled:
        for key in set(json.loads(r["my_deck"])):
            g = card_stats.setdefault(key, {"games": 0, "wins": 0})
            g["games"] += 1
            g["wins"] += r["result"] == "W"
    card_rows = []
    for key, g in card_stats.items():
        c = cards.get(base_key(key), {})
        card_rows.append({"key": key, "name": c.get("Name", key), "cost": c.get("ManaCost"), "type": c.get("Type"),
                          "rarity": c.get("Rarity"), **g, "winrate": g["wins"] / g["games"]})
    card_rows.sort(key=lambda c: (-c["games"], -c["winrate"], c["name"]))

    opp_card_stats = {}
    for r in labeled:
        for key in set(json.loads(r["opp_deck"])):
            g = opp_card_stats.setdefault(key, {"games": 0, "losses": 0})
            g["games"] += 1
            g["losses"] += r["result"] == "L"
    opp_rows = sorted(({"key": k, "name": name(k), **g, "lossrate": g["losses"] / g["games"]}
                       for k, g in opp_card_stats.items()), key=lambda c: (-c["games"], -c["lossrate"]))

    def opp_type(r):
        n = r["opp_cursor_points"]
        return "?" if n is None else ("Bot" if n == 0 else "Human")

    # rank as recorded in each replay, tracked per build since each build has its
    # own ladder; a new entry each time it changes
    rank_history, last_by_build = [], {}
    for r in rows:
        b = r["build"] or "?"
        if r["my_rank"] and last_by_build.get(b) != r["my_rank"]:
            rank_history.append({"build": b, "rank": r["my_rank"], "since": r["played_at"]})
            last_by_build[b] = r["my_rank"]
    if build:
        current_rank = last_by_build.get(build)
    else:
        current_rank = ", ".join(f"{rk} ({b})" for b, rk in last_by_build.items()) or None

    return {
        "build": build, "builds": builds, "latest_build": latest_build,
        "total": len(rows), "labeled": len(labeled), "unlabeled": len(rows) - len(labeled),
        "current_rank": current_rank, "rank_history": rank_history,
        "by_my_rank": group(lambda r: f"{r['my_rank']} ({r['build'] or '?'})"),
        "by_build": group(lambda r: r["build"] or "?"),
        "wins": wins, "losses": len(labeled) - wins,
        "winrate": (wins / len(labeled)) if labeled else None,
        "by_my_commander": [{**g, "name": name(g["key"])} for g in group(lambda r: r["my_commander"])],
        "by_opp_commander": [{**g, "name": name(g["key"])} for g in group(lambda r: r["opp_commander"])],
        "by_opp_rank": group(lambda r: r["opp_rank"]),
        "by_opp_type": group(opp_type),
        "cards": card_rows,
        "opp_cards": opp_rows,
        "onboarding": onboarding_record(),
        "matches": [{**dict(r), "opp_type": opp_type(r),
                     "my_commander_name": name(r["my_commander"]), "opp_commander_name": name(r["opp_commander"]),
                     "my_deck_names": [name(k) for k in json.loads(r["my_deck"])],
                     "opp_deck_names": [name(k) for k in json.loads(r["opp_deck"])]} for r in reversed(rows)],
    }


def cmd_stats(build=None):
    s = compute_stats(db(), build)
    scope = f" [{build}]" if build else ""
    print(f"matches{scope}: {s['total']} ({s['labeled']} labeled, {s['unlabeled']} need a W/L)")
    if s["winrate"] is not None:
        print(f"record: {s['wins']}-{s['losses']}  win rate {s['winrate']:.0%}")
    if s["current_rank"]:
        print(f"rank: {s['current_rank']}  (" +
              ", ".join(f"{h['rank']} on {h['build']} from {h['since'][:10]}" for h in s["rank_history"]) + ")")
    if s["onboarding"]:
        o = s["onboarding"]
        print(f"onboarding bot matches (from game cache): {o['wins']}-{o['losses']} "
              f"({o['wins'] / (o['wins'] + o['losses']):.0%})")
    if s["by_my_commander"]:
        print("\nby my commander")
        for g in s["by_my_commander"]:
            print(f"  {g['name']:<24} {g['wins']}-{g['games'] - g['wins']}  {g['winrate']:.0%}")
    if s["cards"]:
        print("\nmy cards (win rate when in deck)")
        for c in s["cards"]:
            print(f"  {c['name']:<28} {c['games']:>3} games  {c['winrate']:.0%}")
    if s["matches"]:
        print("\nrecent matches")
        for m in s["matches"][:15]:
            print(f"  {m['played_at']}  {m['result'] or '?'}  [{m['build'] or '?'} {m['my_rank']}] {m['my_commander_name']} vs "
                  f"{m['opp_commander_name']} ({m['opp']}, {m['opp_rank']}, {m['opp_type'].lower()})  {m['rounds']} rounds")


# ----------------------------------------------------------------------------- dashboard
def dashboard_html():
    return open(os.path.join(HERE, "dashboard.html")).read()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, dashboard_html(), "text/html")
        if u.path == "/api/stats":
            conn = db()
            for p in game_replays():
                ingest(p, conn)
            build = parse_qs(u.query).get("build", [None])[0] or None
            return self._send(200, json.dumps(compute_stats(conn, build), default=str))
        self._send(404, "{}")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/label":
            q = parse_qs(u.query)
            mid, res = q.get("id", [None])[0], q.get("result", [None])[0]
            conn = db()
            conn.execute("UPDATE matches SET result=? WHERE id=?", (res if res in ("W", "L") else None, mid))
            conn.commit()
            return self._send(200, "{}")
        self._send(404, "{}")


def cmd_serve(port=8787):
    for p in game_replays():
        ingest(p)
    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            sys.exit(f"port {port} is already in use — is another tracker running? "
                     f"Try: python3 tracker.py serve {port + 1}")
        raise
    print(f"dashboard: http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "stats"
    if cmd == "import":
        cmd_import()
    elif cmd == "watch":
        cmd_watch()
    elif cmd == "label":
        cmd_label(args[1], args[2] if len(args) > 2 else None)
    elif cmd == "stats":
        cmd_stats(args[1] if len(args) > 1 else None)
    elif cmd == "serve":
        cmd_serve(int(args[1]) if len(args) > 1 else 8787)
    else:
        print(__doc__)
