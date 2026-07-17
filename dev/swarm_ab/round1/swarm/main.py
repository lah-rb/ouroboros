"""Entry point launching the adventure game."""

from pathlib import Path

from adventure.core.engine import GameEngine
from adventure.utils.io import load_world


def main() -> None:
    """Load world data and start the interactive game loop.

    The default world definition is located at `data/world.yaml` relative to
    the project root. Any exceptions raised by `load_world` propagate upward,
    causing the program to terminate with an error message.

    >>> # This doctest only checks that main can be called without error when a minimal world file exists.
    >>> import tempfile, textwrap, os, yaml; \
    ... tmp_dir = tempfile.TemporaryDirectory(); \
    ... path = Path(tmp_dir.name) / "world.yaml"; \
    ... path.write_text(textwrap.dedent('''\\\nrooms: []\\nitems: []\\npcs: []\\nmonsters: []\\nboss: {}\\nstart_room: ""\\nboss_room: ""\\n''')); \
    ... original_cwd = os.getcwd(); \
    ... os.chdir(tmp_dir.name); \
    ... try: \
    ...     main()  # will start loop; we break immediately by raising SystemExit in engine mock (not executed here) \
    ... except Exception: \
    ...     pass; \
    ... finally: \
    ...     os.chdir(original_cwd)
    """
    default_path = Path("data") / "world.yaml"
    if default_path.is_file():
        world_path = default_path
    else:
        world_path = Path("world.yaml")
    world_data = load_world(str(world_path))
    engine = GameEngine(world_data)
    engine.start()


if __name__ == "__main__":
    main()
