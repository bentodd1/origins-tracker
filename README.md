# Origins TCG tracker

Reads the replay file Origins TCG writes after every match
(`~/Library/Application Support/io.koingames.originstcg.game/LatestMatch_*.replay`),
keeps a copy of each one (the game overwrites it next match), and shows win rate,
per-card and per-commander stats in a local dashboard.

```
python3 tracker.py serve      # dashboard on http://localhost:8787 (also ingests new replays every 5s while open)
python3 tracker.py watch      # headless: ingest each new replay as it appears
python3 tracker.py label W    # mark the latest match (or use the W/L buttons in the dashboard)
python3 tracker.py stats      # text summary
```

## Requirements

- Python 3.9 or newer, nothing else to install.
- macOS or Windows (wherever Origins TCG runs). On Windows use `python` in place of `python3`.

The tracker finds the game's data folder automatically:

| Platform | Folder |
|---|---|
| macOS | `~/Library/Application Support/io.koingames.originstcg.game/` |
| Windows | `%USERPROFILE%\AppData\LocalLow\Koin Games\Origins TCG Demo\` (or `Origins TCG Playtest`, `Origins TCG`) |

If yours is somewhere else, point it at the folder that contains `LatestMatch_*.replay`:

```
ORIGINS_GAME_DIR="/path/to/folder" python3 tracker.py serve
```

Match history is stored in `data/` next to the script and is not committed.

## What the replay contains

`replay_format.py` decodes the game's field-tagged binary. Per match: arena, seed,
location pool, both players' names, ranks, commanders and full 13-card decks, then
40 round records each holding committed placements, a timed event log (draws,
drags/drops, ready clicks, phase advances, emotes) and each player's mouse track.

Bot opponents are marked as such: the replay stores an is-bot flag per player, and a
bot's player id is the literal string `Bot`. Bots also leave no mouse-cursor track.

**The file does not say who won.** Mark each match W/L in the dashboard. This was
checked against seven wins and a conceded loss: every unexplained header field is
identical across all of them, and a concede adds no event of its own — the round
log simply stops. The replay is an input log the game re-simulates from the seed,
so the outcome is never written down.

The one indirect check is the ladder. Each header carries your rank at match
start, so the next match on the same build shows what the previous one did to it;
the dashboard's "Rank after" column surfaces that, and a rise confirms a win.

Cards inside round events are referenced by a per-match instance id, not a deck
slot, so "cards played" stats need the id mapping worked out from several
labeled replays; for now card stats are win rate when the card is in your deck.
