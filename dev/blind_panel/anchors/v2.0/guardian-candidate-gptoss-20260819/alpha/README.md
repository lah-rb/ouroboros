# Text Adventure Game

A fully‑featured, command‑line text adventure written in Python.  
Features include:

- Title screen and help system
- Rich command parser (movement, inventory, combat, dialogue, etc.)
- Turn‑based combat with distinct monster behaviours
- Equipment that modifies player stats
- NPCs with branching dialogue that hint at the final boss’s weakness
- Save/load system using JSON
- A demo world with 8+ rooms, items, NPCs, and a multi‑phase boss

## Getting Started

### Prerequisites

- Python 3.9 or newer
- `pip` (Python package installer)

### Installation

```sh
git clone <repository-url>
cd <repo-directory>
pip install -r requirements.txt
```

### Running the Game

```sh
python main.py
```

The game will start with a title screen. Type `help` at any time to see available commands.

## Project Structure

```
/data
    world.yaml          # World definition (rooms, items, NPCs, monsters)
main.py                 # Entry point (provided by the source‑code flow)
...                     # Other .py modules (game logic, UI, parser, etc.)
pyproject.toml          # Build metadata and dependencies
requirements.txt        # Pinning of runtime dependencies
README.md               # This file
.gitignore              # Standard Python ignores
```

## Contributing

Feel free to open issues or submit pull requests. Follow the coding style enforced by `ruff` and `black`.

## License

This project is licensed under the MIT License – see the LICENSE file for details.
