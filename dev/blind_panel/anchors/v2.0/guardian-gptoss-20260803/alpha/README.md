# Text Adventure Game

A fully‑featured, command‑line text adventure written in Python.  
Features include:

- Title screen and help system
- Rich command parser (movement, inventory, combat, dialogue, etc.)
- Turn‑based combat with distinct monster behaviours
- Equipment that modifies player stats
- NPCs with branching dialogue that hint at the boss’s weakness
- Save/load functionality via JSON
- A demo world with 8 rooms, 5 items, 2 NPCs, 3 regular monsters and a multi‑phase boss

## Requirements

- Python 3.9 or newer
- [PyYAML](https://pyyaml.org/) (installed automatically via the project dependencies)

## Installation

```sh
# Clone the repository
git clone <repo-url>
cd text-adventure

# Install the project and its dependencies
pip install .
```

## Running the Game

```sh
# Using the console script installed above
text-adventure

# Or directly with Python
python main.py
```

## Controls

After the title screen, type commands such as:

- `go north` / `go south` / `go east` / `go west`
- `take <item>`, `drop <item>`, `use <item>`, `examine <item>`
- `equip <weapon|armor>`
- `talk to <npc>`
- `attack <monster>`, `flee`
- `look`, `status`, `help`, `quit`

The `help` command lists all available commands.

## Development

To work on the codebase:

```sh
# Install development dependencies
pip install .[dev]

# Run linting / formatting
ruff check .
black .
```

## License

This project is licensed under the MIT License – see the [LICENSE](LICENSE) file for details.
