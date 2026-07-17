# Text Adventure Game

A Python‑based, turn‑based text adventure game that loads its world from a YAML file.  
Features include:

- Movement through rooms (`go north`, `go south`, …)
- Inventory management (`take`, `drop`, `use`, `examine`)
- NPC interaction with branching dialogue
- Combat system with weapons, armor, healing items, and a multi‑phase final boss
- Save and load of full game state to JSON
- Engaging descriptive text powered by **rich** for colourised output

## Requirements

- Python 3.9 or newer
- Dependencies listed in `pyproject.toml` (PyYAML, rich)

## Installation

```bash
# Clone the repository
git clone <repo-url>
cd <repo-directory>

# Install dependencies (editable install)
pip install -e .
