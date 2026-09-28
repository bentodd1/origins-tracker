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

**Automatic labeling from the results screen (macOS).** When a new replay lands
the tracker takes a screenshot, reads it with the built-in Vision OCR (`ocr.swift`,
compiled on first use), and labels the match if the Victory or Defeat banner is on
screen. It needs **Screen Recording** permission for whatever app hosts the terminal
you run the tracker from (System Settings > Privacy & Security > Screen Recording);
without it macOS hands back a windowless capture and the match simply stays
unlabeled for you to click. Only a whole-line "Victory" or "Defeat" counts, because
"Victory Points" appears on the screen after a loss as well.

The one indirect check is the ladder. Each header carries your rank at match
start, so the next match on the same build shows what the previous one did to it;
the dashboard's "Rank after" column surfaces that, and a rise confirms a win.

## Card instance ids

Round events refer to cards by a per-match instance id. Each deck card gets two
consecutive ids in decklist order, except a legendary, which has one copy. Player
0's ids start at 1 and player 1's at 30; ids past the deck are tokens created
mid-game. Checked on 25 replays: every placement with no lane maps to a Spell
under this scheme (0 misses in 124), and no neighbouring scheme manages that.

This gives win rate **when played**, times played, and average turn per card, for
you and for opponents. Draws are not in the replay: draw order comes from the
seeded shuffle, so only the mulligan and the cards actually played are recorded.

Decks are named from the deck list the client caches (`items.Deck.*`), matched on
the 13 cards, so the "By deck" table uses the names you gave them in game.
