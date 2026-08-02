# The Ashen Keep

A self-contained, turn-based text adventure written in pure Python (no
third-party dependencies). Explore a nine-room ruined keep, fight three
distinct monsters, talk to two ghosts of the place who each drop part
of the same clue, gear up on weapons, armor, and healing draughts, and
face a two-phase boss who is nearly unbeatable unless you've been
listening.

## Running it

Requires Python 3.7+. From this directory:

```
python main.py
```
or
```
python3 main.py
```

The game opens on a title screen ([N]ew Game / [L]oad Game / [Q]uit)
before play begins.

## Commands

```
Movement:
  go north / go south / go east / go west   (or just: n, s, e, w)

Looking around:
  look                    describe your current surroundings again
  examine <thing>         look closely at an item, yourself, or a foe
  status                  show your health, equipment, and location
  inventory               list what you're carrying

Items:
  take <item>             pick something up off the floor
  drop <item>             leave something on the floor
  equip <item>            wield a weapon or wear a piece of armor
  use <item>              drink a potion or use a consumable

People:
  talk to <name>          speak with someone in the room

Combat (once a monster blocks your way):
  attack                  strike with your equipped weapon
  flee                    try to break off and retreat
  use <item> / equip <item>   still work mid-fight, and cost a turn

Other:
  save                    write your progress to savegame.json
  load                    load progress from savegame.json
  help                    show the in-game command reference
  quit                    leave the game (with a confirmation)
```

Typing "help" in-game always shows this reference. Typing "status"
mid-fight also shows the monster you're facing and its remaining HP.

## The world

Nine rooms, fully connected by north/south/east/west exits, centered on
a stairwell landing:

```
                         Throne of Ash
                              |
                          Silent Crypt
                              |
   Library -- Stairwell Landing -- Storeroom
                              |
                        Great Hall
                        /          \
                   Armory          Shattered Shrine
                        \
                    Ruined Courtyard (start)
```

Seven items are scattered through it: a starting Rusty Dagger, an
upgrade Iron Longsword, two tiers of armor (Boiled Leather Vest and
Scaled Breastplate), Healing Draughts, a lore-only Old Journal, and the
**Sunfire Brand** -- the one weapon the final boss actually fears.

Three regular monsters each fight differently:

- **Hollow Sentinel** (Armory) -- a slow, disciplined fighter that
  braces behind its shield every third turn, halving your next hit
  instead of attacking that round.
- **Ravenous Cur** (Storeroom) -- a fast brawler that turns reckless
  once wounded, biting twice in a single turn below half health.
- **Cave Widow** (Crypt) -- hits softer, but its bite has a good
  chance to poison you, ticking extra damage at the start of each
  following round until it wears off.

**Sister Maren** (Shattered Shrine) and **Old Archivist Rell** (High
Library) are both worth talking to more than once -- their dialogue
advances each time, and between the two of them they tell you exactly
what the final boss is weak to and where to find it. The Old Journal
in the Great Hall drops the same hint a third way, for players who
prefer reading over conversation.

## The boss

**The Ashen King** waits on his throne behind the crypt. He is
naturally resistant to ordinary weapons (steel "slides off him"), and
enters a more aggressive second phase once he drops to half health --
his crown cracks, his attack rises, and he gains a chance to strike
twice per round. Face him with an ordinary blade and the fight is
brutal and probably unwinnable. Face him with the Sunfire Brand
equipped and that same resistance flips into a severe weakness --
exactly as Sister Maren and Rell will tell you, if you ask.

Defeating him wins the game. Dying shows an honest defeat screen and
offers a full restart.

## Saving and loading

`save` writes a complete snapshot to `savegame.json` in the current
working directory: player stats, equipped gear, full inventory,
current and previous location, poison status, which monsters have
been defeated, how far each NPC conversation has progressed, and the
live state of every room in the world (remaining items, monster HP,
and boss phase). `load` (in-game or from the title screen) restores
all of it exactly. Saving is disabled mid-fight -- flee first.

## Project layout

```
main.py               entry point
adventure/
  __init__.py
  items.py             Item definitions (weapons, armor, consumables, lore)
  monsters.py          Monster base class + 4 monsters with distinct behaviour
  npc.py                NPC base class + 2 NPCs with staged, branching dialogue
  player.py             Player state, stats, and JSON (de)serialization
  world.py              Room class + the 9-room map
  parser.py             Command parsing, synonyms, and the help text
  game.py                Game class: dispatch, combat resolution, save/load, screens
```
