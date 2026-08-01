# The Sunstone Wyrm

A small, complete text adventure. A wyrm has taken the mountain above
your village; village rumor says steel alone won't be enough to kill it.

## Running it

```
pip install -r requirements.txt
python main.py
```

Python 3.9+ recommended. The only dependency is PyYAML, used to load the
world data.

## Playing

The game opens on a title screen (new game / load game / quit), then
drops you into the village square. Type commands at the `>` prompt.
Type `help` any time for the full list; the short version:

- **Movement:** `go north`, `go south`, `go east`, `go west`, `go up`,
  `go down` -- or just `north` / `n` / `s` / `e` / `w` / `u` / `d`.
- **Items:** `take <item>`, `drop <item>`, `inventory` (`i`),
  `examine <thing>`, `equip <item>`, `unequip <item>`, `use <item>`
  (heals with a potion mid-fight too).
- **People:** `talk to <name>`, then `ask <name> about <topic>` to dig
  into what they know.
- **Combat:** `attack`, `flee`.
- **Other:** `look`, `status`, `map`, `save`, `load`, `help`, `quit`.

`status` shows your health, attack, defense, equipped gear, and current
location at a glance.

## How to win

Three monsters guard specific rooms on the way to the mountain, each
with its own tactics. The Ashwyrm waiting at the end has two phases and
an armored hide that shrugs off ordinary weapons -- unless you're
carrying (and wearing) something the village elder and the tavern's bard
both, in their own way, keep hinting about. Talk to people. Ask them
about things. Explore before you fight.

Dying shows an honest defeat screen and offers a restart; beating the
Ashwyrm shows a victory screen. Either way, the game asks if you want to
play again.

## World data

Rooms, items, NPCs, and monster stats all live in `data/*.yaml`, separate
from the engine in `game/`. To extend the world -- a new room, a new
item, a new line of NPC dialogue -- you only need to edit YAML; no Python
changes required as long as you're reusing the existing monster
behaviors. Adding a monster with genuinely new *behavior* means adding a
small class in `game/monsters.py` (see `DireWolf`, `SkeletalGuardian`,
and `BanditScout` for three different examples) and registering it in
`MONSTER_CLASSES`.

## Saving

`save` writes `savegame.json` next to `main.py`, capturing your stats,
inventory, equipment, location, which monsters are defeated, which NPCs
you've met, and every room's remaining items. `load` restores all of it
(also offered from the title screen).

## Project layout

```
main.py              entry point
game/
  engine.py           command dispatch, the main loop, save/load
  parser.py            turns a typed line into a Command
  world.py               loads data/*.yaml into Room/Item/NPC objects
  player.py                 stats, inventory, equipment
  monsters.py                 monster behaviors + the Ashwyrm boss
  combat.py                     turn-based combat resolution
  ui.py                          title screen, victory/defeat screens
data/
  rooms.yaml            the map
  items.yaml              weapon, armor, two healing items, boss-weakness trinket
  npcs.yaml                  Elder Maren and Rowan
  monsters.yaml                 dire wolf, skeletal guardian, bandit scout, Ashwyrm
```
