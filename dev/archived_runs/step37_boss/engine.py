from models import Player, Command, CommandType


class CombatEngine:
    def __init__(self, game: "GameEngine"):
        self.game = game

    def calculate_damage(self, attacker, defender, is_player: bool) -> int:
        if is_player:
            base = attacker.base_attack
            weapon_id = attacker.equipment.get("weapon")
            if weapon_id and weapon_id in self.game.items:
                base += self.game.items[weapon_id].stats.get("attack_bonus", 0)
        else:
            base = attacker.attack
        # Armor reduction
        if not is_player:
            armor_id = defender.equipment.get("armor")
            if armor_id and armor_id in self.game.items:
                base -= self.game.items[armor_id].stats.get("defense_bonus", 0)
        damage = max(1, base)
        # Boss weakness
        if is_player and hasattr(defender, "id") and defender.id == "shadow_lord":
            if "crystal_shard" in attacker.inventory:
                damage *= 3
        return damage

    def monster_attack(self, monster_id: str) -> list[str]:
        monster = self.game.monsters[monster_id]
        dmg = self.calculate_damage(monster, self.game.player, False)
        self.game.player.health -= dmg
        return [f"{monster.name} attacks you for {dmg} damage."]

    def combat_turn(self, monster_id: str) -> list[str]:
        monster = self.game.monsters[monster_id]
        # Player attacks
        dmg = self.calculate_damage(self.game.player, monster, True)
        monster.health -= dmg
        output = [f"You attack {monster.name} for {dmg} damage."]
        # Check boss phases
        if monster.phases:
            for idx, phase in enumerate(monster.phases):
                threshold = phase.get("health_threshold")
                if (
                    monster.health <= threshold
                    and idx not in self.game.boss_phases_triggered
                ):
                    self.game.boss_phases_triggered.add(idx)
                    if "attack_bonus" in phase:
                        monster.attack += phase["attack_bonus"]
                        output.append(
                            f"{monster.name} grows stronger! Attack +{phase['attack_bonus']}."
                        )
                    if "behavior" in phase:
                        monster.behavior = phase["behavior"]
                        output.append(f"{monster.name} becomes {phase['behavior']}!")
        if monster.health <= 0:
            output.append(f"{monster.name} is defeated!")
            self.game.defeated_monsters.append(monster_id)
            rs = self.game.room_states[self.game.player.location]
            if rs["monster"] == monster_id:
                rs["monster"] = None
            for item_id in monster.loot:
                rs["items"].append(item_id)
            if monster.id == "shadow_lord":
                output.append("You have saved the realm! Congratulations!")
            return output
        # Monster attacks
        monster_atk = self.monster_attack(monster_id)
        output.extend(monster_atk)
        if self.game.player.health <= 0:
            output.append("You have been slain.")
        return output


class GameEngine:
    def __init__(self, world_data: dict):
        self.rooms = world_data["rooms"]
        self.items = world_data["items"]
        self.npcs = world_data["npcs"]
        self.monsters = world_data["monsters"]
        self.start_room = world_data["start_room"]
        self.player = Player(
            location=self.start_room,
            health=20,
            max_health=20,
            base_attack=2,
            inventory=[],
            equipment={"weapon": None, "armor": None},
            flags={},
        )
        self.room_states = {}
        for rid, room in self.rooms.items():
            self.room_states[rid] = {
                "visited": False,
                "items": list(room.items),
                "npcs": list(room.npcs),
                "monster": room.monsters[0] if room.monsters else None,
                "looted": False,
            }
        self.npc_states = {}
        for nid, npc in self.npcs.items():
            self.npc_states[nid] = {
                "dialogue_stage": 0,
                "quest_flags": list(npc.quest_flags),
            }
        self.defeated_monsters = []
        self.playtime_seconds = 0.0
        self.in_combat = False
        self.current_monster = None
        self.boss_phases_triggered = set()
        self.combat_engine = CombatEngine(self)
        # Mark start room visited
        self.room_states[self.start_room]["visited"] = True

    def execute_command(self, cmd: Command) -> list[str]:
        output = []

        def maybe_monster_attack():
            if cmd.type not in (
                CommandType.FLEE,
                CommandType.QUIT,
                CommandType.SAVE,
                CommandType.LOAD,
            ):
                if (
                    self.in_combat
                    and self.current_monster
                    and self.current_monster not in self.defeated_monsters
                ):
                    if cmd.type != CommandType.ATTACK:
                        output.extend(
                            self.combat_engine.monster_attack(self.current_monster)
                        )

        if cmd.type == CommandType.QUIT:
            output.append("Goodbye!")
            maybe_monster_attack()
            return output
        if cmd.type == CommandType.MOVE:
            direction = cmd.args.get("direction")
            if not direction:
                output.append("Go where?")
                maybe_monster_attack()
                return output
            room = self.rooms[self.player.location]
            if direction not in room.connections:
                output.append("You can't go that way.")
                maybe_monster_attack()
                return output
            target_room_id = room.connections[direction]
            self.player.location = target_room_id
            rs = self.room_states[target_room_id]
            rs["visited"] = True
            if rs["monster"] and rs["monster"] not in self.defeated_monsters:
                monster = self.monsters[rs["monster"]]
                if monster.behavior == "aggressive":
                    self.in_combat = True
                    self.current_monster = rs["monster"]
                    output.append(f"A {monster.name} attacks you!")
                elif monster.behavior == "guard":
                    output.append(f"{monster.name} stands guard here.")
                else:
                    output.append(f"{monster.name} is here.")
            else:
                self.in_combat = False
                self.current_monster = None
            output.append(self.describe_room())
            maybe_monster_attack()
            return output
        if cmd.type == CommandType.LOOK:
            output.append(self.describe_room())
            maybe_monster_attack()
            return output
        if cmd.type == CommandType.TAKE:
            item_id = cmd.target
            if not item_id:
                output.append("Take what?")
                maybe_monster_attack()
                return output
            rs = self.room_states[self.player.location]
            if item_id not in rs["items"]:
                output.append("You don't see that here.")
                maybe_monster_attack()
                return output
            rs["items"].remove(item_id)
            self.player.inventory.append(item_id)
            output.append(f"You take the {self.items[item_id].name}.")
            maybe_monster_attack()
            return output
        if cmd.type == CommandType.DROP:
            item_id = cmd.target
            if not item_id:
                output.append("Drop what?")
                maybe_monster_attack()
                return output
            if item_id not in self.player.inventory:
                output.append("You don't have that.")
                maybe_monster_attack()
                return output
            if self.player.equipment.get("weapon") == item_id:
                self.player.equipment["weapon"] = None
            if self.player.equipment.get("armor") == item_id:
                self.player.equipment["armor"] = None
            self.player.inventory.remove(item_id)
            rs = self.room_states[self.player.location]
            rs["items"].append(item_id)
            output.append(f"You drop the {self.items[item_id].name}.")
            maybe_monster_attack()
            return output
        if cmd.type == CommandType.USE:
            item_id = cmd.target
            if not item_id:
                output.append("Use what?")
                maybe_monster_attack()
                return output
            if item_id not in self.player.inventory:
                output.append("You don't have that.")
                maybe_monster_attack()
                return output
            item = self.items[item_id]
            if item.type == "healing":
                heal = item.stats.get("heal_amount", 10)
                self.player.health = min(
                    self.player.max_health, self.player.health + heal
                )
                output.append(f"You use {item.name} and restore {heal} health.")
                self.player.inventory.remove(item_id)
            elif item.type == "weapon":
                old = self.player.equipment["weapon"]
                if old:
                    self.player.inventory.append(old)
                    output.append(f"You unequip {self.items[old].name}.")
                self.player.equipment["weapon"] = item_id
                self.player.inventory.remove(item_id)
                output.append(f"You equip the {item.name}.")
            elif item.type == "armor":
                old = self.player.equipment["armor"]
                if old:
                    self.player.inventory.append(old)
                    output.append(f"You unequip {self.items[old].name}.")
                self.player.equipment["armor"] = item_id
                self.player.inventory.remove(item_id)
                output.append(f"You put on the {item.name}.")
            else:
                output.append(f"You can't use {item.name} right now.")
            maybe_monster_attack()
            return output
        if cmd.type == CommandType.EXAMINE:
            target = cmd.target
            if not target:
                output.append("Examine what?")
                maybe_monster_attack()
                return output
            if target in self.player.inventory:
                item = self.items[target]
                extra = ""
                if self.player.equipment.get("weapon") == target:
                    extra = " (equipped)"
                elif self.player.equipment.get("armor") == target:
                    extra = " (equipped)"
                output.append(f"{item.name}{extra}: {item.description}")
            elif target in self.room_states[self.player.location]["items"]:
                output.append(
                    f"{self.items[target].name}: {self.items[target].description}"
                )
            elif target in self.room_states[self.player.location]["npcs"]:
                npc = self.npcs[target]
                if npc.dialogue:
                    for line in npc.dialogue:
                        output.append(f"{npc.name}: {line}")
                else:
                    output.append(f"{npc.name}: A mysterious figure.")
            elif self.room_states[self.player.location].get("monster") == target:
                monster = self.monsters[target]
                output.append(
                    f"{monster.name}: A {monster.behavior} creature with {monster.health} HP."
                )
            else:
                output.append("You don't see that here.")
            maybe_monster_attack()
            return output
        if cmd.type == CommandType.TALK:
            npc_id = cmd.target
            if not npc_id:
                output.append("Talk to whom?")
                maybe_monster_attack()
                return output
            rs = self.room_states[self.player.location]
            if npc_id not in rs["npcs"]:
                matched_nid = None
                for nid in rs["npcs"]:
                    if self.npcs[nid].name.lower() == npc_id.lower():
                        matched_nid = nid
                        break
                if matched_nid is None:
                    output.append("That person isn't here.")
                    maybe_monster_attack()
                    return output
                npc_id = matched_nid
            npc = self.npcs[npc_id]
            state = self.npc_states[npc_id]
            stage = state["dialogue_stage"]
            if stage < len(npc.dialogue):
                for line in npc.dialogue:
                    output.append(f'{npc.name} says: "{line}"')
                state["dialogue_stage"] = len(npc.dialogue)
                if "knows_weakness" in npc.quest_flags:
                    self.player.flags["knows_weakness"] = True
            else:
                output.append(f"{npc.name} has nothing more to say.")
            maybe_monster_attack()
            return output
        if cmd.type == CommandType.ATTACK:
            if self.in_combat and self.current_monster:
                monster_id = self.current_monster
            else:
                rs = self.room_states[self.player.location]
                monster_id = rs["monster"]
                if not monster_id or monster_id in self.defeated_monsters:
                    output.append("There's nothing to attack.")
                    maybe_monster_attack()
                    return output
                self.in_combat = True
                self.current_monster = monster_id
            combat_output = self.combat_engine.combat_turn(monster_id)
            output.extend(combat_output)
            if monster_id in self.defeated_monsters:
                self.in_combat = False
                self.current_monster = None
            maybe_monster_attack()
            return output
        if cmd.type == CommandType.FLEE:
            if not self.in_combat:
                output.append("You're not in combat.")
            else:
                self.in_combat = False
                self.current_monster = None
                output.append("You flee from combat!")
            maybe_monster_attack()
            return output
        if cmd.type == CommandType.STATUS:
            output.append(f"Health: {self.player.health}/{self.player.max_health}")
            output.append(f"Attack: {self.player.base_attack}")
            maybe_monster_attack()
            return output
        if cmd.type == CommandType.INVENTORY:
            if not self.player.inventory:
                output.append("You are not carrying anything.")
            else:
                output.append("You are carrying:")
                for item_id in self.player.inventory:
                    output.append(f"  {self.items[item_id].name}")
            maybe_monster_attack()
            return output

    def describe_room(self) -> str:
        """Return a formatted multi-line description of the current room."""
        room_id = self.player.location
        room = self.rooms.get(room_id)
        if room is None:
            return "Room: Unknown Room\nStatus: Unknown\nItems: none\nNPCs: none\nMonster: none"

        room_name = room.name
        visited = self.room_states.get(room_id, {}).get("visited", False)
        status = "Visited" if visited else "Unvisited"

        item_names = [self.items[i].name for i in room.items if i in self.items]
        items_str = ", ".join(item_names) if item_names else "none"

        npc_names = [self.npcs[n].name for n in room.npcs if n in self.npcs]
        npcs_str = ", ".join(npc_names) if npc_names else "none"

        monster_id = room.monsters[0] if room.monsters else None
        if monster_id and monster_id in self.monsters:
            monster_name = self.monsters[monster_id].name
        else:
            monster_name = "none"

        lines = [
            f"Room: {room_name}",
            f"Status: {status}",
            f"Items: {items_str}",
            f"NPCs: {npcs_str}",
            f"Monster: {monster_name}",
        ]
        return "\n".join(lines)
