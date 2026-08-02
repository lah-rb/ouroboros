from typing import Dict, List


class Player:
    def __init__(
        self,
        health: int = 20,
        attack: int = 5,
        defense: int = 2,
        inventory: List[str] = None,
        equipment: Dict[str, str] = None,
        location: str = "entrance",
    ):
        self.health = health
        self.attack = attack
        self.defense = defense
        self.inventory = inventory if inventory is not None else []
        self.equipment = (
            equipment if equipment is not None else {"weapon": None, "armor": None}
        )
        self.location = location


class Monster:
    def __init__(self, name: str, hp: int, attack: int, defense: int, behaviour: str):
        self.name = name
        self.hp = hp
        self.attack = attack
        self.defense = defense
        self.behaviour = behaviour


class Item:
    def __init__(
        self, name: str, item_type: str, description: str, stats: Dict[str, int]
    ):
        self.name = name
        self.type = item_type
        self.description = description
        self.stats = stats


class Room:
    def __init__(
        self,
        id: str,
        title: str,
        description: str,
        exits: Dict[str, str],
        items: List[str],
        monsters: List[str],
        npcs: List[str],
    ):
        self.id = id
        self.title = title
        self.description = description
        self.exits = exits
        self.items = items
        self.monsters = monsters
        self.npcs = npcs


class NPC:
    def __init__(
        self, name: str, dialogue: List[Dict[str, str]], talked_once: bool = False
    ):
        self.name = name
        self.dialogue = dialogue
        self.talked_once = talked_once
