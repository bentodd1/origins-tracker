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

**The file does not say who won.** Mark each match W/L in the dashboard. The
tracker stores a few unexplained header flags per match so once a handful are
labeled we can check whether one of them is the result.

Cards inside round events are referenced by a per-match instance id, not a deck
slot, so "cards played" stats need the id mapping worked out from several
labeled replays; for now card stats are win rate when the card is in your deck.
