"""User‑interface helpers for the text adventure.

All functions print human‑readable information to ``stdout`` and return
``None``.  They are deliberately simple – the game engine (``game.py``)
decides what to pass in.  The signatures follow the contracts listed in
the project blueprint; optional arguments have sensible defaults so that
calls with fewer parameters still succeed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from entities import Player, Room, Monster


def display_title() -> None:
    """Render the game’s title screen."""
    title_art = r"""
   _____                 _                     _             
  / ____|               | |                   | |            
 | (___   ___  _ __ ___ | |__   ___ _ __   ___| |_ ___  _ __ 
  \___ \ / _ \| '_ ` _ \| '_ \ / _ \ '__| / __| __/ _ \| '__|
  ____) | (_) | | | | | | |_) |  __/ |    \__ \ || (_) | |   
 |_____/ \___/|_| |_| |_|_.__/ \___|_|    |___/\__\___/|_|   
                                                           
"""
    print(title_art)
    print("Welcome to the Ruins of the Ancient Citadel!")
    print("-" * 55)


def display_help() -> None:
    """Show a brief list of available commands."""
    help_text = """
Available commands:
  move <direction>      – travel north, south, east, west, up or down
  look                  – re‑display the current room description
  inventory (i)         – list items you are carrying
  take <item>           – pick up an item from the floor
  drop <item>           – leave an item in the current room
  equip <item>          – equip a weapon or armor from your inventory
  unequip <slot>        – remove equipped weapon or armor (slot: weapon/armor)
  use <item>            – consume a consumable (e.g., healing potion)
  talk <npc>            – speak with a non‑player character
  attack                – attack the monster in the room (if any)
  flee                  – attempt to escape combat
  save                  – write the current game state to disk
  load                  – restore a previously saved game
  help (h, ?)           – show this help message
  quit (q, exit)        – exit the game
"""
    print(help_text.strip())


def display_status(player: Player) -> None:
    """Print a concise status line for the player."""
    # Basic stats
    health_line = f"Health: {player.health}/{player.max_health}"
    attack_line = f"Attack: {player.attack}"
    defense_line = f"Defense: {player.defense}"
    location_line = f"Location: {player.location}"

    # Equipment
    weapon = player.equipment.get("weapon") or "none"
    armor = player.equipment.get("armor") or "none"
    equip_line = f"Equipped – Weapon: {weapon}, Armor: {armor}"

    # Inventory summary (show count, not full list, to keep line short)
    inv_count = len(player.inventory)
    inventory_line = f"Inventory items: {inv_count}"

    print("\n=== PLAYER STATUS ===")
    print(health_line)
    print(attack_line)
    print(defense_line)
    print(location_line)
    print(equip_line)
    print(inventory_line)
    print("=" * 22)


def display_room(
    room: Room,
    world_state: Optional[Dict[str, Any]] = None,
    *,
    items_lookup: Optional[Dict[str, Any]] = None,
    npcs_lookup: Optional[Dict[str, Any]] = None,
) -> None:
    """Render the description of a room.

    Parameters
    ----------
    room : Room
        The static ``Room`` object loaded from ``world.yaml``.
    world_state : dict, optional
        Mutable state for the current play‑through (as produced by
        ``world.init_world_state``).  If omitted, only the static data is
        displayed.
    items_lookup : dict, optional
        Mapping of item IDs to objects (or at least to a ``name`` attribute)
        for nicer display.  If not supplied, item IDs are shown.
    npcs_lookup : dict, optional
        Mapping of NPC IDs to objects for name lookup.  If omitted,
        NPC IDs are shown.
    """
    print("\n=== ROOM ===")
    print(f"{room.name}")
    print("-" * len(room.name))
    print(room.description)

    # Exits
    if room.exits:
        exits_formatted = ", ".join(sorted(room.exits.keys()))
        print(f"Exits: {exits_formatted}")

    # Dynamic content (items, NPCs, monster) – guard against missing state
    state_room: Dict[str, Any] = {}
    if world_state and "rooms" in world_state:
        state_room = world_state["rooms"].get(room.id, {})

    # Items present in the room (dynamic list overrides static list)
    item_ids: List[str] = state_room.get("items", list(room.items))
    if item_ids:
        if items_lookup:
            item_names = [
                items_lookup[i].name for i in item_ids if i in items_lookup
            ]
        else:
            item_names = item_ids
        print(f"You see: {', '.join(item_names)}")

    # NPCs present
    npc_ids: List[str] = list(room.npcs)
    if npc_ids:
        if npcs_lookup:
            npc_names = [
                npcs_lookup[n].name for n in npc_ids if n in npcs_lookup
            ]
        else:
            npc_names = npc_ids
        print(f"People here: {', '.join(npc_names)}")

    # Monster presence – respect the defeated flag
    monster_id = room.monster
    monster_defeated = state_room.get("monster_defeated", monster_id is None)
    if monster_id and not monster_defeated:
        print(f"A hostile presence looms: {monster_id}")

    print("=" * 22)


def display_combat_turn(
    turn_result: Dict[str, Any],
    player: Player,
    monster: Monster,
) -> None:
    """Narrate the outcome of a single combat round.

    Parameters
    ----------
    turn_result : dict
        The result dictionary returned by ``CombatEngine.take_turn``.
    player : Player
        Current player state (after damage/healing has been applied).
    monster : Monster
        Current monster state (after damage has been applied).
    """
    print("\n--- COMBAT TURN ---")

    # Actions
    p_action = turn_result.get("player_action", "none")
    m_action = turn_result.get("monster_action", "none")
    print(f"You {p_action}.")
    if not turn_result.get("monster_defeated", False):
        print(f"The {monster.name} {m_action}.")

    # Damage / healing reports
    dmg_to_monster = turn_result.get("damage_to_monster", 0)
    dmg_to_player = turn_result.get("damage_to_player", 0)

    if dmg_to_monster:
        print(f"You dealt {dmg_to_monster} damage to the {monster.name}.")
    if dmg_to_player:
        print(f"The {monster.name} hit you for {dmg_to_player} damage.")

    # Phase change (bosses)
    if turn_result.get("phase_change", False):
        print(f"The {monster.name} roars and enters a new, more dangerous phase!")

    # Defeat checks
    if turn_result.get("monster_defeated", False):
        print(f"You have slain the {monster.name}!")
    if turn_result.get("player_defeated", False):
        print("You have been defeated...")

    # Current health summary
    print(f"Your health: {player.health}/{player.max_health}")
    print("-" * 22)


def display_defeat() -> None:
    """Show the game‑over screen."""
    print("\n=== DEFEAT ===")
    print("Your journey ends here. The citadel's secrets remain hidden.")
    print("Better luck next time!")
    print("=" * 22)


def display_victory() -> None:
    """Show the victory screen."""
    print("\n=== VICTORY ===")
    print(
        "With the final boss vanquished, the ancient citadel crumbles behind you."
    )
    print("You have uncovered its mysteries and survived the ordeal!")
    print("=" * 22)


__all__ = [
    "display_title",
    "display_help",
    "display_status",
    "display_room",
    "display_combat_turn",
    "display_defeat",
    "display_victory",
]
