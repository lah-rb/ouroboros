"""NPC definitions for The Ashen Keep.

Both NPCs are reached with the single "talk to <name>" command -- there
is no separate "ask about" verb. Branching is done by advancing a
per-NPC stage counter stored on the player (player.npc_stage[key]) each
time talk() is called, so repeated conversations reveal more of the
story instead of repeating the same line, and the later stages react
to what the player has actually done (items carried, monsters beaten).
"""


class NPC:
    """Base class for a conversable non-player character."""

    def __init__(self, key, name, description):
        self.key = key
        self.name = name
        self.description = description

    def talk(self, player):
        """Return a list of narration lines for this conversation, and
        advance player.npc_stage[self.key] as a side effect.
        """
        raise NotImplementedError


class SisterMaren(NPC):
    """A ghost kneeling in the ruined shrine. Speaks in riddles about
    the Ashen King's weakness to fire without ever naming the item
    outright -- the concept, not the location.
    """

    def __init__(self):
        super().__init__(
            key="sister_maren",
            name="Sister Maren",
            description=(
                "A translucent figure in tattered shrine-robes, kneeling "
                "before a broken altar. She does not seem to notice the "
                "dust settling through her."
            ),
        )

    def talk(self, player):
        stage = player.npc_stage.get(self.key, 0)
        if stage == 0:
            player.npc_stage[self.key] = 1
            return [
                'The ghostly figure lifts her head slowly. "A living soul... it has been so long."',
                '"This keep belongs to the Ashen King now -- once a just lord, now a '
                'cinder-hearted tyrant. He burned this shrine, and everyone in it. Myself included."',
                '"Steel will not save you, wanderer. Whatever blade you carry, it will not be enough."',
            ]
        elif stage == 1:
            player.npc_stage[self.key] = 2
            return [
                '"You want to know how to end him? Then listen well."',
                '"The King wears his death like armor now -- ash and cinder, cold to any '
                'ordinary blade. But flame remembers flame."',
                '"Seek what burns without dying: a brand born of the sun itself. It sleeps '
                "somewhere in this keep, hidden from looters. Find it. Carry it. Wield it "
                'against him."',
            ]
        else:
            if player.weapon == "sunfire_brand" or player.has_item("sunfire_brand"):
                return [
                    "Sister Maren's hollow eyes brighten faintly. \"You carry sunfire in "
                    'your hands, wanderer. He will feel it. Go -- end this."'
                ]
            barks = [
                '"The dead do not forget. Neither should you -- steel will not be enough."',
                '"Somewhere below, the dawn still burns, waiting to be found."',
                '"I would weep for you, if I still could."',
            ]
            idx = (stage - 2) % len(barks)
            player.npc_stage[self.key] = stage + 1
            return [barks[idx]]


class ArchivistRell(NPC):
    """An old scholar squatting in the library. Speaks plainly and,
    unlike Sister Maren, eventually names the item and its location
    outright -- the "if you talk to everyone" safety net.
    """

    def __init__(self):
        super().__init__(
            key="rell",
            name="Old Archivist Rell",
            description=(
                "A stooped old man surrounded by teetering stacks of "
                "scorched books, muttering to himself as he catalogs the "
                "keep's ruin one page at a time."
            ),
        )

    def talk(self, player):
        stage = player.npc_stage.get(self.key, 0)
        if stage == 0:
            player.npc_stage[self.key] = 1
            return [
                'The old man startles, then relaxes. "Oh -- a visitor. Not a ghost, not '
                'ash. How refreshing."',
                "\"I catalog what's left of this place. It was a proud keep once, before "
                "its lord let grief curdle into something monstrous. Now he squats on that "
                'throne, more cinder than man."',
            ]
        elif stage == 1:
            player.npc_stage[self.key] = 2
            return [
                '"You mean to face him? Then you\'ll want the Sunfire Brand."',
                '"I catalogued every relic before the ash spread. The Brand was forged to '
                "hold captured dawn-light -- too bright, too dangerous, so they entombed it "
                'with the dead in the crypt below, so its light could not tempt looters."',
                '"It is the one thing I know of that scars true fire back into that ashen '
                'husk of a king. Ordinary steel just... slides off him."',
            ]
        else:
            if "cave_widow" in player.defeated_monsters:
                line = (
                    "\"You crossed the Widow's crypt and lived -- good. Mind his second "
                    "wind; he burns hotter once he's badly wounded.\""
                )
            else:
                line = '"Mind the stair down to the crypt. I hear something large nests there now."'
            player.npc_stage[self.key] = stage + 1
            return [line]


_NPC_CLASSES = {
    "sister_maren": SisterMaren,
    "rell": ArchivistRell,
}


def create_npc(key):
    """Instantiate a fresh NPC by key."""
    return _NPC_CLASSES[key]()
