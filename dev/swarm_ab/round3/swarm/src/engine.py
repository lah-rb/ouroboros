"""Core game loop, command dispatch, state updates and integration."""

from __future__ import annotations
from typing import Dict, Any
from src.models import GameState, Player, Item, Room, NPC, Monster
from src.loader import load_world
from src.parser import parse_command, Command
from src.combat import initiate_combat, CombatResult
from src.save_load import save_game, load_game

class GameEngine:
    """Runs the interactive adventure.

    The engine loads world data, maintains a GameState and processes
    player commands. Command vocabulary and routing are enumerated in
    ``_dispatch`` documentation.

    Public methods:
        start(): Begin the input loop; returns when the player quits.
    """

    def __init__(self, yaml_path: str = "world.yaml"):
        """Create a new engine instance.

        Args:
            yaml_path: Path to the world definition file.
        """
        # Load raw data
        raw = load_world(yaml_path)

        # Helper to build Item objects
        def make_item(d):
            return Item(
                id=d["id"],
                name=d["name"],
                description=d.get("description", ""),
                type=d.get("type", "")
            )

        # Build rooms
        rooms = {}
        for rid, rdata in raw.get("rooms", {}).items():
            room_items = [make_item(i) for i in rdata.get("items", [])]
            room_npcs = []
            for nd in rdata.get("npcs", []):
                room_npcs.append(NPC(
                    id=nd["id"],
                    name=nd["name"],
                    location=rid,
                    dialogue=nd.get("dialogue", []),
                    dialogue_index=0
                ))
            monster_obj = None
            if "monster" in rdata and rdata["monster"] is not None:
                m = rdata["monster"]
                monster_obj = Monster(
                    id=m["id"],
                    name=m["name"],
                    health=m["health"],
                    max_health=m["max_health"],
                    attack=m["attack"],
                    description=m.get("description", "")
                )
            rooms[rid] = Room(
                id=rid,
                name=rdata.get("name", ""),
                description=rdata.get("description", ""),
                connections=rdata.get("connections", {}),
                items=room_items,
                npcs=room_npcs,
                monster=monster_obj
            )

        # Build NPC dictionary (global reference)
        npcs = {}
        for room in rooms.values():
            for npc in room.npcs:
                npcs[npc.id] = npc

        # Build Monster dictionary (global reference)
        monsters = {}
        for room in rooms.values():
            if room.monster:
                monsters[room.monster.id] = room.monster

        # Build player
        pdata = raw.get("player", {})
        player = Player(
            location=pdata.get("location", ""),
            health=pdata.get("health", 100),
            max_health=pdata.get("max_health", 100),
            attack=pdata.get("attack", 5),
            defense=pdata.get("defense", 0),
            inventory=[make_item(i) for i in pdata.get("inventory", [])],
            equipped={'weapon': None, 'armor': None}
        )

        # Assemble GameState
        self.state = GameState(
            player=player,
            rooms=rooms,
            npcs=npcs,
            monsters=monsters
        )
        self._running = True

    def start(self) -> None:
        """Enter the main command loop.

        The loop repeatedly:
          1. Reads raw input from ``input()``.
          2. Calls :func:`parse_command` to obtain a Command.
          3. Dispatches based on ``Command.type`` using the mapping
             described in ``_dispatch``.
          4. Prints outcome messages and updates GameState.

        The loop terminates when a QUIT command is processed or when
        the player dies.

        >>> # doctest: +SKIP (interactive)
        """
        while self._running:
            try:
                raw = input("> ").strip()
                if not raw:
                    continue
                cmd = parse_command(raw)
                self._dispatch(cmd)
                if self.state.player.health <= 0:
                    print("You have died.")
                    break
            except Exception as e:
                # Any unexpected error should be reported but not crash the loop
                print(f"Error: {e}")

    def _dispatch(self, cmd: Command) -> None:
        """Route a parsed command to its handler.

        Mapping (command type → handler signature):
          MOVE      -> self._handle_move(direction: str)
          TAKE      -> self._handle_take(item_id: str)
          DROP      -> self._handle_drop(item_id: str)
          USE       -> self._handle_use(item_id: str)
          EXAMINE   -> self._handle_examine(target_id: str)
          TALK      -> self._handle_talk(npc_id: str)
          ATTACK    -> self._handle_attack(monster_id: str)
          FLEE      -> self._handle_flee()
          LOOK      -> self._handle_look()
          STATUS    -> self._handle_status()
          HELP      -> self._handle_help()
          SAVE      -> self._handle_save(filepath: str)
          LOAD      -> self._handle_load(filepath: str)
          QUIT      -> self._handle_quit()

        Args:
            cmd: Parsed Command object.

        Returns:
            None. Handlers may raise to abort the loop.
        """
        t = cmd.type
        if t.name == "MOVE":
            self._handle_move(cmd.target)
        elif t.name == "TAKE":
            self._handle_take(cmd.target)
        elif t.name == "DROP":
            self._handle_drop(cmd.target)
        elif t.name == "USE":
            self._handle_use(cmd.target)
        elif t.name == "EXAMINE":
            self._handle_examine(cmd.target)
        elif t.name == "TALK":
            self._handle_talk(cmd.target)
        elif t.name == "ATTACK":
            self._handle_attack(cmd.target)
        elif t.name == "FLEE":
            self._handle_flee()
        elif t.name == "LOOK":
            self._handle_look()
        elif t.name == "STATUS":
            self._handle_status()
        elif t.name == "HELP":
            self._handle_help()
        elif t.name == "SAVE":
            self._handle_save(cmd.target)
        elif t.name == "LOAD":
            self._handle_load(cmd.target)
        elif t.name == "QUIT":
            self._handle_quit()
        else:
            raise ValueError(f"Unknown command type: {t}")

    def _handle_move(self, direction: str) -> None:
        current_room = self.state.rooms[self.state.player.location]
        if direction in current_room.connections:
            new_room_id = current_room.connections[direction]
            self.state.player.location = new_room_id
            print(f"You move {direction}.")
            self._handle_look()
        else:
            print("You can't go that way.")

    def _handle_take(self, item_id: str) -> None:
        room = self.state.rooms[self.state.player.location]
        for i, item in enumerate(room.items):
            if item.id == item_id:
                self.state.player.inventory.append(item)
                del room.items[i]
                print(f"You take the {item.name}.")
                return
        print("There is no such item here.")

    def _handle_drop(self, item_id: str) -> None:
        inv = self.state.player.inventory
        for i, item in enumerate(inv):
            if item.id == item_id:
                room = self.state.rooms[self.state.player.location]
                room.items.append(item)
                del inv[i]
                print(f"You drop the {item.name}.")
                return
        print("You don't have that item.")

    def _handle_use(self, item_id: str) -> None:
        inv = self.state.player.inventory
        for i, item in enumerate(inv):
            if item.id == item_id:
                if item.type.lower() == "healing":
                    # Simple healing logic; assume heal amount 20
                    heal_amount = 20
                    new_health = min(self.state.player.health + heal_amount,
                                     self.state.player.max_health)
                    self.state.player.health = new_health
                    del inv[i]
                    print(f"You use the {item.name} and recover {heal_amount} health.")
                elif item.type.lower() == "weapon":
                    self.state.player.equipped['weapon'] = item
                    print(f"You equip the {item.name} as a weapon.")
                elif item.type.lower() == "armor":
                    self.state.player.equipped['armor'] = item
                    print(f"You equip the {item.name} as armor.")
                else:
                    print(f"You use the {item.name}, but nothing happens.")
                return
        print("You don't have that item.")

    def _handle_examine(self, target_id: str) -> None:
        # Check room items
        room = self.state.rooms[self.state.player.location]
        for item in room.items:
            if item.id == target_id:
                print(f"{item.name}: {item.description}")
                return
        # Check inventory
        for item in self.state.player.inventory:
            if item.id == target_id:
                print(f"{item.name} (in inventory): {item.description}")
                return
        # Check NPCs
        for npc in room.npcs:
            if npc.id == target_id:
                print(f"{npc.name}: {npc.dialogue[0] if npc.dialogue else '...'}")
                return
        # Check monster
        if room.monster and room.monster.id == target_id:
            m = room.monster
            print(f"{m.name}: {m.description} (HP: {m.health}/{m.max_health})")
            return
        print("You see nothing special about that.")

    def _handle_talk(self, npc_id: str) -> None:
        room = self.state.rooms[self.state.player.location]
        for npc in room.npcs:
            if npc.id == npc_id:
                if npc.dialogue:
                    line = npc.dialogue[npc.dialogue_index % len(npc.dialogue)]
                    npc.dialogue_index += 1
                    print(f"{npc.name} says: \"{line}\"")
                else:
                    print(f"{npc.name} has nothing to say.")
                return
        print("There is no one here by that name.")

    def _handle_attack(self, monster_id: str) -> None:
        room = self.state.rooms[self.state.player.location]
        monster = room.monster
        if not monster or monster.id != monster_id:
            print("No such monster here.")
            return

        result = initiate_combat(self.state.player, monster)
        if isinstance(result, CombatResult):
            # Assume CombatResult has attributes: player_dead, monster_dead, player_damage, monster_damage
            if getattr(result, "player_dead", False):
                self.state.player.health = 0
                print("You have been slain by the monster.")
                return
            if getattr(result, "monster_dead", False):
                room.monster = None
                del self.state.monsters[monster.id]
                print(f"You have defeated {monster.name}!")
            else:
                # Update health values if provided
                self.state.player.health = getattr(result, "player_health", self.state.player.health)
                monster.health = getattr(result, "monster_health", monster.health)
                print(f"The combat continues. Your health: {self.state.player.health}.")
        else:
            print("Combat could not be resolved.")

    def _handle_flee(self) -> None:
        # Simple flee implementation: just prints a message.
        print("You attempt to flee... but there's nowhere to run!")

    def _handle_look(self) -> None:
        room = self.state.rooms[self.state.player.location]
        print(f"\n{room.name}\n{room.description}")
        exits = ", ".join(room.connections.keys())
        print(f"Exits: {exits if exits else 'none'}")
        if room.items:
            items = ", ".join(item.name for item in room.items)
            print(f"You see: {items}")
        if room.npcs:
            npcs = ", ".join(npc.name for npc in room.npcs)
            print(f"People here: {npcs}")
        if room.monster:
            m = room.monster
            print(f"A hostile {m.name} is here! (HP: {m.health}/{m.max_health})")

    def _handle_status(self) -> None:
        p = self.state.player
        print(f"Health: {p.health}/{p.max_health}")
        print(f"Attack: {p.attack}  Defense: {p.defense}")
        if p.inventory:
            inv = ", ".join(item.name for item in p.inventory)
            print(f"Inventory: {inv}")
        else:
            print("Inventory: empty")
        weapon = p.equipped.get('weapon')
        armor = p.equipped.get('armor')
        print(f"Equipped Weapon: {weapon.name if weapon else 'none'}")
        print(f"Equipped Armor: {armor.name if armor else 'none'}")

    def _handle_help(self) -> None:
        commands = [
            "move <direction>", "take <item_id>", "drop <item_id>", "use <item_id>",
            "examine <target_id>", "talk <npc_id>", "attack <monster_id>", "flee",
            "look", "status", "help", "save <filepath>", "load <filepath>", "quit"
        ]
        print("Available commands:")
        for cmd in commands:
            print(f"  {cmd}")

    def _handle_save(self, filepath: str) -> None:
        save_game(self.state, filepath)
        print(f"Game saved to {filepath}.")

    def _handle_load(self, filepath: str) -> None:
        self.state = load_game(filepath)
        print(f"Game loaded from {filepath}.")

    def _handle_quit(self) -> None:
        print("Goodbye!")
        self._running = False
