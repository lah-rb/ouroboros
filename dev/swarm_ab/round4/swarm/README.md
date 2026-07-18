# Text Adventure Game

A Python‑based, turn‑based text adventure that loads its world from YAML files.  
Features include:

- Movement between rooms (`go north`, `go south`, …)
- Inventory management (`take`, `drop`, `use`, `examine`)
- NPC dialogue with branching hints
- Combat system with weapons, armor, healing items, and a multi‑phase final boss
- Save and load of full game state to JSON
- Engaging descriptive text and clear combat narration

## Installation

```sh
# Clone the repository
git clone https://github.com/yourusername/text-adventure-game.git
cd text-adventure-game

# Create a virtual environment (optional but recommended)
python -m venv .venv
source .venv/bin/activate   # on Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
