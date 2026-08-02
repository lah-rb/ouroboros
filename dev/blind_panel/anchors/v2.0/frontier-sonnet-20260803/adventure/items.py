"""Item definitions for The Ashen Keep.

Items are referenced everywhere else by their string ``key``. Rooms and
the player inventory store lists of these keys (duplicates allowed, so
carrying two Healing Draughts is just the key appearing twice). The
Item objects themselves are stateless definitions shared from the
ITEMS registry below -- looking one up never mutates it.
"""


class Item:
    """A static definition of a single kind of item in the game world.

    item_type is one of: "weapon", "armor", "consumable", "misc".
      - weapon: value is an attack bonus, added when equipped.
      - armor: value is a defense bonus, added when equipped.
      - consumable: heal_amount is HP restored when used, then consumed.
      - misc: flavor/lore items that can be examined but not equipped
        or used.
    """

    def __init__(self, key, name, description, item_type,
                 value=0, heal_amount=0, boss_weakness=False):
        self.key = key
        self.name = name
        self.description = description
        self.item_type = item_type
        self.value = value
        self.heal_amount = heal_amount
        self.boss_weakness = boss_weakness

    def __repr__(self):
        return f"Item({self.key!r})"


ITEMS = {
    "rusty_dagger": Item(
        key="rusty_dagger",
        name="Rusty Dagger",
        item_type="weapon",
        value=2,
        description=(
            "A pitted iron dagger, more rust than edge. It won't win you "
            "any songs, but it's better than swinging your fists."
        ),
    ),
    "iron_longsword": Item(
        key="iron_longsword",
        name="Iron Longsword",
        item_type="weapon",
        value=6,
        description=(
            "A well-balanced longsword, notched from old battles but "
            "still sharp enough to matter. Whoever wielded it last knew "
            "how to fight."
        ),
    ),
    "sunfire_brand": Item(
        key="sunfire_brand",
        name="Sunfire Brand",
        item_type="weapon",
        value=3,
        boss_weakness=True,
        description=(
            "A short, warm blade whose edge glows faintly, like a sliver "
            "of captured sunrise sealed in steel. It was never meant to "
            "be buried in the dark."
        ),
    ),
    "scaled_breastplate": Item(
        key="scaled_breastplate",
        name="Scaled Breastplate",
        item_type="armor",
        value=5,
        description=(
            "Overlapping iron scales on a leather backing, dented in a "
            "few places but fundamentally sound. Heavy, but it will turn "
            "aside a blade that would otherwise find your ribs."
        ),
    ),
    "boiled_leather_vest": Item(
        key="boiled_leather_vest",
        name="Boiled Leather Vest",
        item_type="armor",
        value=2,
        description=(
            "Stiff, hardened leather, cinched with cracked straps. Not "
            "much protection, but far better than bare skin."
        ),
    ),
    "healing_draught": Item(
        key="healing_draught",
        name="Healing Draught",
        item_type="consumable",
        heal_amount=15,
        description=(
            "A small glass vial of something bitter and faintly "
            "luminous. It smells medicinal in a way that suggests it "
            "still works, ruin or no ruin."
        ),
    ),
    "old_journal": Item(
        key="old_journal",
        name="Old Journal",
        item_type="misc",
        description=(
            "A water-stained journal, most of its pages illegible. The "
            "last legible entry reads: 'He does not sleep. He does not "
            "age. But I swear the crypt below glows sometimes, like "
            "dawn trapped in stone.'"
        ),
    ),
}


def get_item(key):
    """Look up an Item definition by key, or None if it doesn't exist."""
    return ITEMS.get(key)
