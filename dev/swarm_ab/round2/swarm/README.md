# Text Adventure Game

A Python command‑line text adventure that loads its world from a YAML file, supports inventory management, NPC interaction, turn‑based combat, and a multi‑phase final boss. The game can be saved to and loaded from JSON.

## Features

- **YAML world definition** – rooms, items, NPCs, monsters, and connections are described in `data/world.yaml`.
- **Command parser** – movement (`go north`), inventory (`take`, `drop`, `use`, `examine`), combat (`attack`, `flee`), and utility commands (`look`, `status`, `help`, `quit`).
- **Turn‑based combat** – weapons, armor, healing items, monster behaviors, and a two‑phase boss with a hidden weakness.
- **Save/Load** – full game state is persisted to JSON via the `save_load` module.
- **Rich narrative** – descriptive text for rooms, actions, and combat.

## Installation

```sh
# Clone the repository
git clone <repository-url>
cd <repo-directory>

# Create a virtual environment (optional but recommended)
python -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate

# Install dependencies and the package in editable mode
pip install -e .
