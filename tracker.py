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
  python3 tracker.py stats [build] [human|bot]   # win rate and card stats, optionally narrowed
  python3 tracker.py serve [port]    # local dashboard (default http://localhost:8787)
"""
import errno
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
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


def load_named_decks():
    """Deck names from the inventory the client caches: [(name, frozenset(card keys))].
    Commander and tower entries are dropped so the set matches a replay decklist.
    Every cached snapshot is read, so an older version of an edited deck still matches."""
    decks, seen = [], set()

    def add(name, keys):
        cards = frozenset(k for k in keys if k and not k.startswith(("H", "Tower")))
        if name and cards and (name, cards) not in seen:
            seen.add((name, cards))
            decks.append((name, cards))

    for g in GAME_DIRS:
        for f in sorted(glob.glob(os.path.join(g, "beamable", "cache", "*", "*", "*", "*.json")), key=os.path.getmtime, reverse=True):
            try:
                d = json.load(open(f))
            except Exception:
                continue
            if not (isinstance(d, dict) and "items" in d and "currencies" in d):
                continue
            for grp in d["items"]:
                if not grp.get("id", "").startswith("items.Deck."):
                    continue
                for it in grp.get("items", []):
                    props = {p["name"]: p["value"] for p in it.get("properties", [])}
                    try:
                        cfg = json.loads(props.get("Config") or "{}")
                    except Exception:
                        continue
                    add(cfg.get("DisplayName"), [c.get("CardKey") for c in cfg.get("Cards", [])])
    return decks


def deck_name(cards, named):
    """Name for a 13-card list: exact match, else the closest named deck if it
    shares at least 10 cards (marked as edited), else None."""
    cards = frozenset(cards)
    best, best_n = None, 0
    for name, ref in named:
        if ref == cards:
            return name
        n = len(ref & cards)
        if n > best_n:
            best, best_n = name, n
    return f"{best} (edited)" if best and best_n >= 10 else None


def base_key(variant_key):
    return variant_key.split("_V")[0]


_CARD_DB = None


def card_db():
    global _CARD_DB
    if _CARD_DB is None:
        _CARD_DB = load_card_db()[0]
    return _CARD_DB


# Card instance ids inside a replay. Each deck card gets two consecutive ids in
# decklist order, except a legendary, which has a single copy. Player 0's ids
# start at 1 and player 1's at 30; ids past the deck are tokens made mid-game.
# Verified on 25 replays: every no-lane placement maps to a Spell under this
# scheme (0 misses in 124) and under no neighbouring one.
INSTANCE_BASE = {0: 1, 1: 30}


def expand_deck(deck):
    out = []
    for key in deck:
        c = card_db().get(base_key(key), {})
        single = c.get("Rarity") == "Legendary" or base_key(key).endswith(("_MC", "_SC"))
        out += [key] * (1 if single else 2)
    return out


def card_for_instance(player_index, instance_id, expanded):
    j = (instance_id or -1) - INSTANCE_BASE.get(player_index, 10 ** 9)
    return expanded[j] if 0 <= j < len(expanded) else None


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
        # The running build's log is written at launch and at scene changes, so
        # it is the one whose timestamp is nearest the replay's on either side.
        # Anything more than a few hours away belongs to an older session.
        t = os.path.getmtime(replay_path)
        best, best_dt = None, 6 * 3600
        for log in glob.glob(os.path.expanduser("~/Library/Logs/Koin Games/*/Player.log")):
            dt = abs(os.path.getmtime(log) - t)
            if dt < best_dt:
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
            "beamable_id": p[0],           # "Bot" for a bot opponent
            "name": p[1],
            "flag2": p.get(2),             # is-bot flag
            "flag4": p.get(4),
            "rank": p.get(7),
            "avatar": p.get(8),
            "commander": p.get(11),
            "deck": deck,
        })
    me = next((p for p in players if p["name"] == me_name), players[0])
    opp = next((p for p in players if p is not me), None)

    rounds = d[4]
    expanded = {p["index"]: expand_deck(p["deck"]) for p in players}
    placements = []   # (round, player, instance id, lane, slot, card key or None for a token)
    for ri, r in enumerate(rounds):
        for e in r.get(0, []):
            if e.get(0) == COMMIT_PLACE:
                pi = e.get(2)
                placements.append((ri, pi, e.get(50), e.get(51), e.get(53),
                                   card_for_instance(pi, e.get(50), expanded.get(pi, []))))
    # Mouse-cursor samples per player. A bot has no mouse, so zero samples over a
    # whole match marks the opponent as a bot.
    for p in players:
        p["cursor_points"] = sum(len(e.get(1, [])) for r in rounds for e in r.get(2, []) if e.get(0) == p["index"])
    conf = d[2]
    return {
        "file": os.path.basename(path),
        "played_at": played_at,
        "build": detect_build(path),
        "me": me, "opp": opp, "my_index": me["index"],
        "arena": conf.get(0), "seed": conf.get(1), "location_pool": conf.get(37),
        "header_flag": d.get(1),           # always 0 so far, wins and a concede alike; not the result
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
    for col, typ in (("build", "TEXT"), ("my_cursor_points", "INTEGER"), ("opp_cursor_points", "INTEGER"),
                     ("result_source", "TEXT"), ("my_index", "INTEGER"), ("screen_lines", "TEXT")):
        if col not in cols:
            c.execute(f"ALTER TABLE matches ADD COLUMN {col} {typ}")
    if "card_key" not in {r["name"] for r in c.execute("PRAGMA table_info(placements)")}:
        c.execute("ALTER TABLE placements ADD COLUMN card_key TEXT")
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
        conn.execute("UPDATE matches SET my_index=? WHERE id=? AND my_index IS NULL", (rec["my_index"], rec["file"]))
        if not conn.execute("SELECT 1 FROM placements WHERE match_id=? AND card_key IS NOT NULL", (rec["file"],)).fetchone():
            conn.execute("DELETE FROM placements WHERE match_id=?", (rec["file"],))
            insert_placements(conn, rec)
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
    conn.execute("UPDATE matches SET my_index=? WHERE id=?", (rec["my_index"], rec["file"]))
    insert_placements(conn, rec)
    # a screen-only record of the same match (banner seen before/after the file landed)
    twin = conn.execute("SELECT id, result, screen_lines FROM matches WHERE id LIKE 'screen_%' AND result IS NOT NULL "
                        "AND abs(strftime('%s', played_at) - strftime('%s', ?)) < 90", (rec["played_at"],)).fetchone()
    if twin:
        conn.execute("UPDATE matches SET result=?, result_source='screen', screen_lines=? WHERE id=? AND result IS NULL",
                     (twin["result"], twin["screen_lines"], rec["file"]))
        conn.execute("DELETE FROM matches WHERE id=?", (twin["id"],))
    conn.commit()
    return rec


def insert_placements(conn, rec):
    conn.executemany(
        "INSERT INTO placements (match_id, round, player, instance_id, lane, slot, card_key) VALUES (?,?,?,?,?,?,?)",
        [(rec["file"], *p) for p in rec["placements"]])


OCR_BIN = os.path.join(HERE, "ocr")
OCR_SRC = os.path.join(HERE, "ocr.swift")


DEBUG_DIR = os.path.join(DATA_DIR, "debug")


OCR_WIDTH = 1400   # captures are shrunk to this before OCR: the banner is huge, and it keeps polling cheap


def ocr_screen(keep_as=None):
    """Text on the main display as [(text, height)], height being the text's
    height as a fraction of the screen (macOS only; needs Screen Recording
    permission for the terminal). Returns [] if anything fails.
    keep_as: if set, the capture and the text read are kept under data/debug/
    with that name so a failed read can be inspected. Never committed."""
    if sys.platform != "darwin":
        return []
    if not os.path.exists(OCR_BIN) and os.path.exists(OCR_SRC):
        subprocess.run(["swiftc", "-O", OCR_SRC, "-o", OCR_BIN], capture_output=True)
    if not os.path.exists(OCR_BIN):
        return []
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        shot = f.name
    try:
        subprocess.run(["screencapture", "-x", shot], capture_output=True, timeout=10)
        subprocess.run(["sips", "-Z", str(OCR_WIDTH), shot], capture_output=True, timeout=10)
        out = subprocess.run([OCR_BIN, shot], capture_output=True, text=True, timeout=30).stdout
        lines = []
        for ln in out.splitlines():
            h, _, text = ln.partition("\t")
            if text.strip():
                try:
                    lines.append((text.strip(), float(h)))
                except ValueError:
                    lines.append((ln.strip(), 0.0))
        if keep_as:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            shutil.copy(shot, os.path.join(DEBUG_DIR, keep_as + ".png"))
            with open(os.path.join(DEBUG_DIR, keep_as + ".txt"), "w") as fh:
                fh.write("\n".join(f"{h:.3f}  {t}" for t, h in lines))
        return lines
    except Exception:
        return []
    finally:
        try:
            os.unlink(shot)
        except OSError:
            pass


BANNER_MIN_HEIGHT = 0.035   # the results banner is far taller than any menu or chat text


def result_on_screen(lines):
    """W/L if the results-screen banner is among the OCR lines, else None.
    Whole-line match only ("Victory Points" is on the screen after a loss too),
    and the word has to be banner-sized so text in some other window never counts."""
    for item in lines:
        text, h = item if isinstance(item, tuple) else (item, 1.0)
        w = re.sub(r"[^a-z]", "", text.lower())
        if h >= BANNER_MIN_HEIGHT and w == "victory":
            return "W"
        if h >= BANNER_MIN_HEIGHT and w == "defeat":
            return "L"
    return None


def game_running():
    if sys.platform != "darwin":
        return True
    r = subprocess.run(["pgrep", "-f", "MacOS/Origins TCG"], capture_output=True)
    return r.returncode == 0


WATCH_BIN = os.path.join(HERE, "screenwatch")
WATCH_SRC = os.path.join(HERE, "screenwatch.swift")
SCREEN = {"banner": None, "banner_at": 0.0, "lines": [], "frames": 0}   # shared with auto_label


def screen_frames(interval=2.0):
    """Yield (time, [(text, height)]) frames from the long-running screen reader."""
    if not os.path.exists(WATCH_BIN) and os.path.exists(WATCH_SRC):
        subprocess.run(["swiftc", "-O", WATCH_SRC, "-o", WATCH_BIN], capture_output=True)
    if not os.path.exists(WATCH_BIN):
        return
    proc = subprocess.Popen([WATCH_BIN, str(interval), str(OCR_WIDTH)], stdout=subprocess.PIPE, text=True)
    try:
        cur, t = [], 0
        for ln in proc.stdout:
            ln = ln.rstrip("\n")
            if ln.startswith("FRAME"):
                cur, t = [], int(ln.split()[1] or 0)
            elif ln == "END":
                yield t, cur
                if not game_running():
                    return          # stop reading the screen while the game is closed
            else:
                h, _, text = ln.partition("\t")
                try:
                    cur.append((text, float(h)))
                except ValueError:
                    pass
    finally:
        proc.kill()


def screen_poll_loop(interval=2.0, idle_interval=10.0, quiet=False):
    """For builds that no longer write a replay file: watch the screen for the
    Victory/Defeat banner and record a match from that alone. Runs forever; the
    screen reader only runs while the game does. Every line of text seen in the
    20 s after the banner is stored with the match, so opponent name and rank
    change can be mined from it later."""
    if not quiet:
        print("screen watch on: matches are recorded from the results screen when no replay file appears")
    while True:
        if not game_running():
            time.sleep(idle_interval)
            continue
        collecting = None    # (result, started, lines) while gathering the results screen
        for t, lines in screen_frames(interval):
            SCREEN["frames"] += 1
            res = result_on_screen(lines)
            if collecting:
                collecting[2].extend(text for text, _ in lines)
                if time.time() - collecting[1] > 20:
                    record_screen_match(collecting[0], collecting[2])
                    collecting = None
                continue
            if res and time.time() - SCREEN["banner_at"] > 90:
                SCREEN.update(banner=res, banner_at=time.time(), lines=[text for text, _ in lines])
                collecting = (res, time.time(), [text for text, _ in lines])
        time.sleep(idle_interval)


def opponent_from_screen(lines):
    """The results screen shows '<me>  VS  <opponent>'; the opponent is the line after VS."""
    for i, ln in enumerate(lines[:-1]):
        if re.sub(r"[^a-z]", "", ln.lower()) == "vs":
            nxt = lines[i + 1].strip()
            if nxt and not re.fullmatch(r"(?i)victory!?|defeat!?|next|view board|home", nxt):
                return nxt.title() if nxt.isupper() else nxt
    return None


def record_screen_match(res, lines):
    """A match known only from its results screen. Merged into the replay's row
    if one arrives within 90 s (see ingest)."""
    conn = db()
    now = datetime.now()
    mid = f"screen_{now:%Y-%m-%d_%H-%M-%S}"
    builds = [detect_build(p) for p in game_replays()] or [None]
    uniq = []
    for ln in lines:
        if ln not in uniq:
            uniq.append(ln)
    opp = opponent_from_screen(uniq)
    conn.execute(
        "INSERT INTO matches (id, played_at, me, opp, my_deck, opp_deck, rounds, result, result_source, imported_at, build, screen_lines) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, now.isoformat(sep=" ")[:19], None, opp, "[]", "[]", None, res, "screen", now.isoformat(sep=" "),
         builds[0] or current_build_guess(), json.dumps(uniq)))
    conn.commit()
    print(f"[{now:%H:%M:%S}] results screen: {res} vs {opp or '?'} (no replay file; recorded from the screen)")
    return mid


def current_build_guess():
    """Which build is running, from its log being the newest one."""
    logs = glob.glob(os.path.expanduser("~/Library/Logs/Koin Games/*/Player.log"))
    if not logs:
        return None
    newest = max(logs, key=os.path.getmtime)
    return os.path.basename(os.path.dirname(newest)).replace("Origins TCG", "").strip() or "Release"


def auto_label(match_id, attempts=12, interval=2.0):
    """Try for about half a minute to read the results screen and label the match.
    The first, middle and last captures of a failed run are kept for inspection."""
    if db().execute("SELECT 1 FROM matches WHERE id=? AND result IS NOT NULL", (match_id,)).fetchone():
        return None   # already labeled (e.g. merged from the screen poller)
    if SCREEN["frames"]:
        # the screen poller is running: wait for it to see the banner (or to have seen it just before the file landed)
        deadline = time.time() + attempts * interval
        while time.time() < deadline:
            if abs(SCREEN["banner_at"] - time.time()) < 120 and SCREEN["banner"]:
                conn = db()
                conn.execute("UPDATE matches SET result=?, result_source='screen', screen_lines=? WHERE id=? AND result IS NULL",
                             (SCREEN["banner"], json.dumps(SCREEN["lines"]), match_id))
                conn.commit()
                print(f"[{datetime.now():%H:%M:%S}] results screen read: {SCREEN['banner']}")
                return SCREEN["banner"]
            time.sleep(1.0)
        print(f"[{datetime.now():%H:%M:%S}] could not read the results screen; mark it W/L in the dashboard")
        return None
    shutil.rmtree(DEBUG_DIR, ignore_errors=True)   # captures from the previous run
    for i in range(attempts):
        keep = f"attempt{i:02d}" if i in (0, attempts // 2, attempts - 1) else None
        res = result_on_screen(ocr_screen(keep_as=keep))
        if res:
            shutil.rmtree(DEBUG_DIR, ignore_errors=True)   # only a failed read keeps its captures
            conn = db()
            conn.execute("UPDATE matches SET result=?, result_source='screen' WHERE id=? AND result IS NULL", (res, match_id))
            conn.commit()
            print(f"[{datetime.now():%H:%M:%S}] results screen read: {res}")
            return res
        time.sleep(interval)
    print(f"[{datetime.now():%H:%M:%S}] could not read the results screen; mark it W/L in the dashboard")
    return None


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


def cmd_watch(interval=1.0, quiet=False):
    conn = db()
    seen = set(os.path.basename(p) for p in game_replays())
    for p in game_replays():
        ingest(p, conn)
    if not quiet:
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
                      f"({rec['rounds']} rounds)")
                auto_label(rec["file"])
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


def opponent_type(r):
    """Bot / Human / ? for a match row. Player field 2 is the game's own is-bot flag
    (a bot's id is also the literal "Bot"); the cursor count is a fallback for rows
    imported before the flag was understood."""
    if r["opp_flag2"]:
        return "Bot"
    n = r["opp_cursor_points"]
    return "?" if n is None else ("Bot" if n == 0 else "Human")


def compute_stats(conn, build=None, opp=None):
    """Stats for all matches, optionally narrowed to one build (Demo / Playtest / ...)
    and to one kind of opponent (Human / Bot)."""
    cards, _ = load_card_db()
    all_rows = conn.execute("SELECT * FROM matches ORDER BY played_at").fetchall()
    builds = sorted({r["build"] or "?" for r in all_rows})
    latest_build = (all_rows[-1]["build"] or "?") if all_rows else None
    rows_build = [r for r in all_rows if not build or (r["build"] or "?") == build]
    rows = [r for r in rows_build if not opp or opponent_type(r) == opp]
    labeled = [r for r in rows if r["result"]]
    wins = sum(1 for r in labeled if r["result"] == "W")

    def name(key):
        c = cards.get(base_key(key or ""))
        return c["Name"] if c else key

    def group(keyfn, source=None):
        out = {}
        for r in (labeled if source is None else source):
            k = keyfn(r)
            if k is None:
                continue
            g = out.setdefault(k, {"games": 0, "wins": 0})
            g["games"] += 1
            g["wins"] += r["result"] == "W"
        return sorted(({"key": k, **v, "winrate": v["wins"] / v["games"]} for k, v in out.items()),
                      key=lambda g: (-g["games"], -g["winrate"]))

    # record per deck: same commander and same 13 cards = same deck
    named = load_named_decks()
    deck_stats = {}
    for r in rows:
        deck_cards = json.loads(r["my_deck"] or "[]")
        if not deck_cards:
            continue          # known from the results screen only
        k = (r["my_commander"], tuple(sorted(deck_cards)))
        d = deck_stats.setdefault(k, {"games": 0, "wins": 0, "losses": 0, "unlabeled": 0,
                                      "human_w": 0, "human_l": 0, "bot_w": 0, "bot_l": 0,
                                      "first": r["played_at"], "last": r["played_at"], "cards": deck_cards})
        d["last"] = r["played_at"]
        if not r["result"]:
            d["unlabeled"] += 1
            continue
        d["games"] += 1
        won = r["result"] == "W"
        d["wins" if won else "losses"] += 1
        bot = bool(r["opp_flag2"]) or r["opp_cursor_points"] == 0
        d[("bot" if bot else "human") + ("_w" if won else "_l")] += 1
    deck_rows, unnamed = [], 0
    for (cmd, deck_cards), d in sorted(deck_stats.items(), key=lambda kv: kv[1]["first"]):
        nm = deck_name(deck_cards, named)
        if not nm:
            unnamed += 1
            nm = f"{name(cmd)} deck {unnamed}"
        deck_rows.append({"name": nm, "commander": name(cmd), **{k: v for k, v in d.items() if k != "cards"},
                          "winrate": (d["wins"] / d["games"]) if d["games"] else None,
                          "cards": sorted((name(c) for c in d["cards"]),
                                          key=lambda n: n)})
    deck_rows.sort(key=lambda d: (-d["games"], d["name"]))

    card_stats = {}
    for r in labeled:
        for key in set(json.loads(r["my_deck"])):
            g = card_stats.setdefault(key, {"games": 0, "wins": 0})
            g["games"] += 1
            g["wins"] += r["result"] == "W"
    # What was actually played. A turn is four round records; the first placements
    # land in round 4, which is turn 1.
    result_of = {r["id"]: r["result"] for r in labeled}
    my_index = {r["id"]: (r["my_index"] or 0) for r in labeled}
    played, opp_played = {}, {}
    if result_of:
        q = "SELECT match_id, round, player, card_key FROM placements WHERE card_key IS NOT NULL"
        for pr in conn.execute(q):
            mid = pr["match_id"]
            if mid not in result_of:
                continue
            mine = pr["player"] == my_index[mid]
            g = (played if mine else opp_played).setdefault(pr["card_key"], {"matches": {}, "plays": 0, "turns": []})
            g["plays"] += 1
            g["turns"].append(pr["round"] // 4)
            g["matches"][mid] = result_of[mid]

    def played_cols(g, lose=False):
        if not g:
            return {"played_games": 0, "played_wins": 0, "played_winrate": None, "plays": 0, "avg_turn": None}
        res = list(g["matches"].values())
        hits = sum(1 for x in res if x == ("L" if lose else "W"))
        return {"played_games": len(res), "played_wins": hits, "played_winrate": hits / len(res),
                "plays": g["plays"], "avg_turn": sum(g["turns"]) / len(g["turns"])}

    card_rows = []
    for key, g in card_stats.items():
        c = cards.get(base_key(key), {})
        card_rows.append({"key": key, "name": c.get("Name", key), "cost": c.get("ManaCost"), "type": c.get("Type"),
                          "rarity": c.get("Rarity"), **g, "winrate": g["wins"] / g["games"],
                          **played_cols(played.get(key))})
    card_rows.sort(key=lambda c: (-c["played_games"], -(c["played_winrate"] or 0), c["name"]))

    opp_card_stats = {}
    for r in labeled:
        for key in set(json.loads(r["opp_deck"])):
            g = opp_card_stats.setdefault(key, {"games": 0, "losses": 0})
            g["games"] += 1
            g["losses"] += r["result"] == "L"
    opp_rows = sorted(({"key": k, "name": name(k), **g, "lossrate": g["losses"] / g["games"],
                        **played_cols(opp_played.get(k), lose=True)}
                       for k, g in opp_card_stats.items()), key=lambda c: (-c["games"], -c["lossrate"]))

    def opp_type(r):
        # Player field 2 is the game's own is-bot flag (True for a bot, whose id is
        # also the literal string "Bot"). The cursor count is a fallback for
        # rows imported before the flag was understood.
        if r["opp_flag2"]:
            return "Bot"
        n = r["opp_cursor_points"]
        return "?" if n is None else ("Bot" if n == 0 else "Human")

    # rank as recorded in each replay, tracked per build since each build has its
    # own ladder; a new entry each time it changes
    rank_history, last_by_build = [], {}
    for r in rows_build:   # the ladder does not care who the opponent was
        b = r["build"] or "?"
        if r["my_rank"] and last_by_build.get(b) != r["my_rank"]:
            rank_history.append({"build": b, "rank": r["my_rank"], "since": r["played_at"]})
            last_by_build[b] = r["my_rank"]
    if build:
        current_rank = last_by_build.get(build)
    else:
        current_rank = ", ".join(f"{rk} ({b})" for b, rk in last_by_build.items()) or None

    # The header rank is the rank at match start, so the next match on the same
    # build reveals what this one did to it. Uses all matches, not just the
    # filtered ones, so the tab filter never hides the following match.
    rank_after = {}
    prev_by_build = {}
    for r in all_rows:
        b = r["build"] or "?"
        if b in prev_by_build:
            rank_after[prev_by_build[b]] = r["my_rank"]
        prev_by_build[b] = r["id"]

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
        "opp": opp,
        "by_opp_type": group(opp_type, [r for r in rows_build if r["result"]]),
        "by_deck": deck_rows,
        "cards": card_rows,
        "opp_cards": opp_rows,
        "onboarding": onboarding_record(),
        "matches": [{**dict(r), "opp_type": opp_type(r), "rank_after": rank_after.get(r["id"]),
                     "my_commander_name": name(r["my_commander"]), "opp_commander_name": name(r["opp_commander"]),
                     "my_deck_names": [name(k) for k in json.loads(r["my_deck"])],
                     "opp_deck_names": [name(k) for k in json.loads(r["opp_deck"])]} for r in reversed(rows)],
    }


def cmd_stats(build=None, opp=None):
    s = compute_stats(db(), build, opp)
    scope = (f" [{build}]" if build else "") + (f" [vs {opp.lower()}s]" if opp else "")
    print(f"matches{scope}: {s['total']} ({s['labeled']} labeled, {s['unlabeled']} need a W/L)")
    if s["winrate"] is not None:
        print(f"record: {s['wins']}-{s['losses']}  win rate {s['winrate']:.0%}")
    if s["by_opp_type"]:
        print("vs " + ", vs ".join(f"{g['key'].lower()}s {g['wins']}-{g['games'] - g['wins']} ({g['winrate']:.0%})"
                                   for g in s["by_opp_type"]))
    if s["current_rank"]:
        print(f"rank: {s['current_rank']}  (" +
              ", ".join(f"{h['rank']} on {h['build']} from {h['since'][:10]}" for h in s["rank_history"]) + ")")
    if s["onboarding"]:
        o = s["onboarding"]
        print(f"onboarding bot matches (from game cache): {o['wins']}-{o['losses']} "
              f"({o['wins'] / (o['wins'] + o['losses']):.0%})")
    if s["by_deck"]:
        print("\nby deck")
        for d in s["by_deck"]:
            wr = f"{d['winrate']:.0%}" if d["winrate"] is not None else "-"
            print(f"  {d['name']:<24} {d['wins']}-{d['losses']}  {wr:>4}   "
                  f"vs humans {d['human_w']}-{d['human_l']}, vs bots {d['bot_w']}-{d['bot_l']}")
    if s["by_my_commander"]:
        print("\nby my commander")
        for g in s["by_my_commander"]:
            print(f"  {g['name']:<24} {g['wins']}-{g['games'] - g['wins']}  {g['winrate']:.0%}")
    if s["cards"]:
        print("\nmy cards, in games I played them   games  record  win rate  avg turn")
        for c in s["cards"]:
            if c["played_games"]:
                print(f"  {c['name']:<32} {c['played_games']:>5}  {c['played_wins']:>2}-{c['played_games'] - c['played_wins']:<3}  "
                      f"{c['played_winrate']:>7.0%}  {c['avg_turn']:>8.1f}")
            else:
                print(f"  {c['name']:<32}     0  not played in {c['games']} games it was in the deck")
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
            q = parse_qs(u.query)
            build = q.get("build", [None])[0] or None
            opp = q.get("opp", [None])[0] or None
            return self._send(200, json.dumps(compute_stats(conn, build, opp if opp in ("Human", "Bot") else None), default=str))
        self._send(404, "{}")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/label":
            q = parse_qs(u.query)
            mid, res = q.get("id", [None])[0], q.get("result", [None])[0]
            conn = db()
            conn.execute("UPDATE matches SET result=?, result_source=? WHERE id=?",
                         (res if res in ("W", "L") else None, "manual" if res in ("W", "L") else None, mid))
            conn.commit()
            return self._send(200, "{}")
        self._send(404, "{}")


def cmd_serve(port=8787):
    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            sys.exit(f"port {port} is already in use — is another tracker running? "
                     f"Try: python3 tracker.py serve {port + 1}")
        raise
    print(f"dashboard: http://localhost:{port}")
    threading.Thread(target=cmd_watch, kwargs={"quiet": True}, daemon=True).start()
    if sys.platform == "darwin":
        threading.Thread(target=screen_poll_loop, daemon=True).start()
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
        # stats [build] [human|bot], in either order
        opts = [a for a in args[1:]]
        opp = next((a.capitalize() for a in opts if a.lower() in ("human", "bot")), None)
        bld = next((a for a in opts if a.lower() not in ("human", "bot")), None)
        cmd_stats(bld, opp)
    elif cmd == "serve":
        cmd_serve(int(args[1]) if len(args) > 1 else 8787)
    else:
        print(__doc__)
