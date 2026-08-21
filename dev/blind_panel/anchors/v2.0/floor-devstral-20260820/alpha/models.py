from typing import Dict, List, Optional

class Direction:
    NORTH = "north"
    SOUTH = "south"
    EAST = "east"
    WEST = "west"
    UP = "up"
    DOWN = "down"

class Player:
    def __init__(self, health: int = 100, attack: int = 10, defense: int = 5):
        self.health = health
        self.attack = attack
        self.defense = defense
        self.equipped = {}  # type: Dict[str, Item]

class Monster:
    def __init__(self, name: str, health: int, attack: int, behavior: str):
        self.name = name
        self.health = health
        self.attack = attack
        self.behavior = behavior

class Item:
    def __init__(self, item_id: str, name: str, item_type: str, description: str, stats: Dict[str, int]):
        self.id = item_id
        self.name = name
        self.type = item_type
        self.description = description
        self.stats = stats

class Room:
    def __init__(self, room_id: str, title: str, description: str, connections: Dict[str, str], items: List[str], monsters: List[str], npcs: List[str]):
        self.id = room_id
        self.title = title
        self.description = description
        self.connections = connections  # type: Dict[Direction, str]
        self.items = items  # type: List[str]  # item_ids
        self.monsters = monsters  # type: List[str]  # monster_ids
        self.npcs = npcs  # type: List[str]  # npc_ids

class NPC:
    def __init__(self, npc_id: str, name: str, dialogue: List[Dict[str, str]]):
        self.id = npc_id
        self.name = name
        self.dialogue = dialogue  # type: List[Dict[str, str]]
