# Ouroboros Thin Shim — Claude Operating Instructions

This document tells Claude how to run Ouroboros interactively inside the Claude chat environment using the file-based thin shim. Claude acts as the model — reading prompts, reasoning about them, and writing responses.

---

## Quick Start

### 1. Setup

```bash
# Extract the project
cd /home/claude && unzip -qo ouroboros.zip

# Install dependencies
pip install httpx json-repair pydantic pyyaml jinja2 markdown-it-py --break-system-packages -q

# Install CUE binary (if included in upload)
tar xzf cue_v0_16_0_linux_amd64_tar.gz && mv cue /usr/local/bin/

# Install pytest for verification
pip install pytest --break-system-packages -q

# Verify
cd ouroboros && python3 -m pytest tests/ -x -q
```

### 2. Create a Mission

```bash
cd /home/claude/ouroboros

python3 ouroboros.py mission create \
    --mission_config claude_challenge \
    --working-dir /home/claude/test_project \
    --llmvp-endpoint "http://localhost:8555/graphql"
```

---

## The Core Problem: Background Processes Die

Claude's environment kills background processes between bash tool invocations. You cannot launch the agent in one bash call and respond in the next — the agent is dead before you get there.

### What Works: Launch-Feed-Resume

The agent, shim, and an auto-feeder thread all run in one `timeout` call. The feeder has a `classify` function that recognizes prompts and responds automatically. When it encounters a prompt type it doesn't recognize, it returns `None` and the feeder stops — the agent blocks waiting for a response, and the process eventually times out or hits `max_cycles`.

Between runs, you read the unrecognized prompt, write a genuine response, add a new classifier rule to the feeder, and relaunch. The feeder script grows incrementally as the agent reveals new prompt types.

Mission state persists to `.agent/mission.json`, so the agent resumes from where it left off on every restart.

---

## The Test Pattern: Iterative Discovery

This is the actual operating procedure. You build the classifier one prompt type at a time, reacting to each new prompt cold.

### Step 1: First Launch — See What Comes

```bash
cd /home/claude/ouroboros
rm -f /home/claude/ouro_out.txt /home/claude/ouro_in.txt

timeout 45 python3 -c "
import sys, os, threading, asyncio, logging, time, re, json
sys.path.insert(0, '.')
logging.basicConfig(level=logging.WARNING)
from http.server import HTTPServer
import thin_shim
thin_shim.IO_DIR = '/home/claude'

class ReusableServer(HTTPServer):
    allow_reuse_address = True
server = ReusableServer(('127.0.0.1', 8555), thin_shim.GraphQLHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()

call_count = 0

def classify(prompt):
    # Start empty — every prompt is unknown
    return None

def auto_feed():
    global call_count
    out_path = '/home/claude/ouro_out.txt'
    in_path = '/home/claude/ouro_in.txt'
    while True:
        if os.path.exists(out_path) and not os.path.exists(in_path):
            time.sleep(0.2)
            with open(out_path) as f:
                prompt = f.read()
            call_count += 1
            resp = classify(prompt)
            if resp is None:
                print(f'[{call_count}] UNKNOWN — stopping', flush=True)
                # Save for review
                with open(f'/home/claude/prompt_{call_count}.txt', 'w') as f:
                    f.write(prompt)
                return
            with open(in_path, 'w') as f:
                f.write(resp)
            print(f'[{call_count}] responded ({len(resp)} chars)', flush=True)
        time.sleep(0.2)

threading.Thread(target=auto_feed, daemon=True).start()

from agent.effects.local import LocalEffects
from agent.loop import run_agent
effects = LocalEffects(
    working_directory='/home/claude/test_project',
    llmvp_endpoint='http://localhost:8555/graphql',
    trace_prompts=True,
)
try:
    result = asyncio.run(run_agent(
        mission_id='', effects=effects,
        flows_dir='flows', prompts_dir='prompts',
        max_cycles=1,
    ))
    print(f'RESULT: {result.status}')
except Exception as e:
    print(f'STOPPED: {e}')
server.shutdown()
" 2>&1

# Read the prompt it stopped on
cat /home/claude/prompt_1.txt
```

The agent will stop on the first inference call. Read the prompt. This is your first unbiased look at what the model sees.

### Step 2: Write a Response, Add a Rule

After reading the prompt, write your genuine response and add a classifier rule:

```python
def classify(prompt):
    # Rule 1: Architecture design (discovered on first launch)
    if 'Design a Project Architecture' in prompt:
        return read_response('arch.txt')  # your prepared response

    return None  # still unknown for everything else
```

Save your response to a file, update the classify function, and relaunch. The feeder auto-delivers the architecture response and stops on the next unknown prompt.

### Step 3: Repeat

Each launch advances one prompt further. The cycle is:

1. **Launch** → feeder handles all known prompts → stops on unknown
2. **Read** the unknown prompt (saved to `/home/claude/prompt_N.txt`)
3. **React** — write your genuine response as the model would
4. **Add** a classifier rule for that prompt type
5. **Relaunch** → feeder now handles one more type → stops on the next unknown
6. **Report** — tell the user what you saw, what was clear, what was confusing

This is the point. Each new prompt is experienced cold. The classifier grows as a record of every prompt type the agent produces, in the order they appear.

### What You Learn at Each Step

After each prompt, report:
- **What type** of prompt this is (architecture, code gen, verification, etc.)
- **What it's asking** — is the instruction clear?
- **What format** it expects — is the expected output obvious?
- **What context** is provided — is it sufficient to respond well?
- **What's confusing** — any ambiguity, missing info, or misleading examples?

This feedback is the real output of a thin shim session. The files produced are secondary.

---

## Cycle Counting

The agent loop uses `max_cycles` to control how many work flows execute. Mission_control (the orchestrator) runs for free — it doesn't count toward the limit. Only dispatched flows (design_and_plan, file_ops, project_ops, etc.) count.

With `max_cycles=1`:
- Mission_control runs → dispatches one work flow
- The work flow executes (writes files, runs validation)
- The work flow tail-calls back to mission_control
- Mission_control runs again (free) to record the result and verify completion
- Mission_control dispatches the next task but hits the budget → stops

Each `max_cycles=1` run does one complete round-trip. Files are written and results are recorded. The agent stops in a clean, resumable state.

**Note:** After the budget is spent, the next task may be marked `in_progress` because mission_control prepared the dispatch before hitting the limit. On the next restart, stale-detection resets it to `pending`. This is a known edge — the work was not lost, just the bookkeeping is one step behind.

---

## Prompt Types Reference

These are discovered iteratively. Listed here for reference after the first full run.

| Prompt Type | How to Recognize | Response Format |
|---|---|---|
| Architecture design | "Design a Project Architecture" | Fenced JSON with execution, modules, interfaces, data_shapes, creation_order |
| Research planner | "research planner" | JSON with search queries |
| Plan generation | "planning module", "JSON array" | Fenced JSON array with description, flow, inputs, depends_on |
| Functional goals | "functional capabilities" | JSON array of capability strings |
| Director reasoning | "Your Analysis", "step by step" | 3-5 sentences of prose analysis |
| decide_flow menu | "Choose ONE" + flow options | `{"choice": "flow_name"}` |
| select_task menu | "Select the task" + hex IDs | `{"choice": "<12-char-hex>"}` |
| Menu retry | "previous response was not valid" | `{"choice": "option_name"}` |
| Project setup | "project setup module" | Config files in `=== FILE: path ===` fences |
| Code generation | "TARGET FILE", "code generation module" | Code in `=== FILE: path ===` fences |
| Verification | "code review verification" | `{"verified": true, "confidence": "high", "issues": []}` |
| Environment detection | "environment detection" | JSON with validation commands per extension |

---

## Checking Status Between Turns

```bash
# Mission state summary
cd /home/claude/ouroboros && python3 ouroboros.py mission status \
    --working-dir /home/claude/test_project

# Task details
python3 -c "
import json
with open('/home/claude/test_project/.agent/mission.json') as f:
    m = json.load(f)
print(f'Architecture: {\"SET\" if m.get(\"architecture\") else \"null\"}')
print(f'Goals: {len(m.get(\"goals\", []))}')
for t in m.get('plan', []):
    print(f'  [{t[\"status\"]:12s}] {t.get(\"flow\",\"?\")} | {t[\"description\"][:60]}')
"

# What files has the agent created?
find /home/claude/test_project -not -path '*/.agent/*' -type f

# Review logged prompts
ls /home/claude/prompt_*.txt
```

## Restarting Cleanly

```bash
# Clean IO files (always do this before relaunch)
rm -f /home/claude/ouro_out.txt /home/claude/ouro_in.txt

# Reset mission (start completely fresh)
rm -rf /home/claude/test_project/.agent

# Resume existing mission (just relaunch — state is persisted)
# Nothing to do; the next launch picks up from .agent/mission.json
```

---

## Why This Matters

When Claude operates the thin shim, it experiences the agent's prompts firsthand — the same context, menus, and formatting that any local model would see. The iterative discovery pattern ensures each prompt is encountered cold, without foreknowledge of what's coming. This enables:

- **Genuine prompt quality feedback**: Each prompt is evaluated on first sight — "this instruction is ambiguous", "this context block is missing the flow name", "the expected format isn't clear from the examples"
- **Gap discovery**: Unknown prompts reveal prompt types that aren't documented or that behave differently than expected
- **Format validation**: Writing responses exercises the exact parsing pipeline — if the format is wrong, the agent rejects it and retries
- **Flow graph tracing**: Watching the classifier grow reveals the actual execution order, which may differ from the designed flow graph
- **Completion gate testing**: Verification prompts appear only after file writes succeed, confirming the write→verify pipeline works end-to-end
