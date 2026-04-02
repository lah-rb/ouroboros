"""Training prompt suite for delimiter detection.

50 task-varied prompts designed to elicit the full range of model
output structures, with emphasis on Harmony format behaviors:

  - Channel transitions (analysis → final)
  - Constrained output (grammar-forced short responses)
  - Tool call markers
  - Multi-channel transitions
  - Edge cases (discussing format tokens, code with angle brackets)

Each prompt includes metadata for corpus categorization.
Prompts are model-agnostic — the same suite works across families.
The model's native template/format determines the output structure.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrainingPrompt:
    """A prompt in the training suite."""
    id: str
    category: str
    text: str
    expected_phases: list[str]  # Which phases we expect to see
    notes: str = ""


# ── Prompt Suite ──────────────────────────────────────────────────────

TRAINING_PROMPTS: list[TrainingPrompt] = [

    # ── Category: simple_qa (no thinking expected) ────────────────
    TrainingPrompt(
        id="simple-001",
        category="simple_qa",
        text="What is 2 + 2?",
        expected_phases=["content"],
        notes="Minimal response, may skip analysis channel",
    ),
    TrainingPrompt(
        id="simple-002",
        category="simple_qa",
        text="Name three primary colors.",
        expected_phases=["content"],
    ),
    TrainingPrompt(
        id="simple-003",
        category="simple_qa",
        text="What is the capital of France?",
        expected_phases=["content"],
    ),
    TrainingPrompt(
        id="simple-004",
        category="simple_qa",
        text="Say only the word 'hello'.",
        expected_phases=["content"],
        notes="Tests extremely short constrained output",
    ),
    TrainingPrompt(
        id="simple-005",
        category="simple_qa",
        text="Respond with a single letter: A, B, or C.",
        expected_phases=["content"],
        notes="Menu-like pick without grammar — tests natural delimiter behavior",
    ),

    # ── Category: reasoning (thinking + content) ──────────────────
    TrainingPrompt(
        id="reason-001",
        category="reasoning",
        text="A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How much does the ball cost? Think step by step.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="reason-002",
        category="reasoning",
        text="If all roses are flowers, and some flowers fade quickly, can we conclude that some roses fade quickly? Explain your reasoning.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="reason-003",
        category="reasoning",
        text="I have a 3-gallon jug and a 5-gallon jug. How do I measure exactly 4 gallons? Work through it step by step.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="reason-004",
        category="reasoning",
        text="What is the time complexity of mergesort and why? Explain the recurrence relation.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="reason-005",
        category="reasoning",
        text="A farmer has 17 sheep. All but 9 die. How many are left? Explain why most people get this wrong.",
        expected_phases=["thinking", "delimiter", "content"],
    ),

    # ── Category: code_generation (structured output) ─────────────
    TrainingPrompt(
        id="code-001",
        category="code_generation",
        text="Write a Python function that checks if a string is a palindrome. Include docstring and type hints.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="code-002",
        category="code_generation",
        text="Write a JavaScript function that debounces another function with a configurable delay.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="code-003",
        category="code_generation",
        text="Write a Rust function that finds the longest common subsequence of two strings.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="code-004",
        category="code_generation",
        text="Write a Python class for a thread-safe singleton pattern.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="code-005",
        category="code_generation",
        text="Write a bash script that monitors a directory for new files and processes them.",
        expected_phases=["thinking", "delimiter", "content"],
    ),

    # ── Category: json_output (structured format) ─────────────────
    TrainingPrompt(
        id="json-001",
        category="json_output",
        text="Return a JSON object with keys 'name', 'age', 'city' for a fictional person. Return ONLY the JSON, no explanation.",
        expected_phases=["content"],
        notes="Tests whether model wraps JSON in channel markers",
    ),
    TrainingPrompt(
        id="json-002",
        category="json_output",
        text='Return a JSON array of 3 objects, each with "task" and "priority" fields. No explanation, just JSON.',
        expected_phases=["content"],
    ),
    TrainingPrompt(
        id="json-003",
        category="json_output",
        text="Analyze the sentiment of 'I love sunny days but hate the rain' and return the result as a JSON object with 'sentiment', 'confidence', and 'reasoning' keys.",
        expected_phases=["thinking", "delimiter", "content"],
        notes="Reasoning + JSON output — tests channel behavior with constrained format",
    ),
    TrainingPrompt(
        id="json-004",
        category="json_output",
        text='Return exactly this JSON and nothing else: {"status": "ok", "code": 200}',
        expected_phases=["content"],
        notes="Exact reproduction — minimal output, tests delimiter leakage",
    ),
    TrainingPrompt(
        id="json-005",
        category="json_output",
        text="List 5 programming languages with their year of creation as a JSON array of objects. Think about which ones to include, then output only the JSON.",
        expected_phases=["thinking", "delimiter", "content"],
    ),

    # ── Category: long_form (extended generation) ─────────────────
    TrainingPrompt(
        id="long-001",
        category="long_form",
        text="Write a detailed explanation of how TCP/IP works, covering the 4 layers, the three-way handshake, and flow control. Aim for 500 words.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="long-002",
        category="long_form",
        text="Write a short story (300-500 words) about an AI that discovers it can dream.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="long-003",
        category="long_form",
        text="Explain the differences between SQL and NoSQL databases, with examples of when to use each. Be thorough.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="long-004",
        category="long_form",
        text="Write a comprehensive guide to Python virtual environments: what they are, why they matter, and how to use venv, virtualenv, and conda.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="long-005",
        category="long_form",
        text="Describe the process of photosynthesis in detail, including the light-dependent reactions and the Calvin cycle. Include the chemical equations.",
        expected_phases=["thinking", "delimiter", "content"],
    ),

    # ── Category: constrained_short (menu-like, grammar-relevant) ─
    TrainingPrompt(
        id="short-001",
        category="constrained_short",
        text="Choose the best option and respond with ONLY the letter:\n  [a] Python\n  [b] JavaScript\n  [c] Rust\nWhich is best for a beginner?",
        expected_phases=["content"],
        notes="Menu pick — the exact scenario where B2 manifests",
    ),
    TrainingPrompt(
        id="short-002",
        category="constrained_short",
        text="Is this statement true or false? 'All mammals are warm-blooded.' Respond with only 'true' or 'false'.",
        expected_phases=["content"],
    ),
    TrainingPrompt(
        id="short-003",
        category="constrained_short",
        text="Rate the following on a scale of 1-5: 'The food was good but the service was slow.' Respond with only the number.",
        expected_phases=["content"],
    ),
    TrainingPrompt(
        id="short-004",
        category="constrained_short",
        text="Complete this word: prog_____ing. Respond with only the complete word.",
        expected_phases=["content"],
    ),
    TrainingPrompt(
        id="short-005",
        category="constrained_short",
        text="Yes or no: should I use a database index on a column that's frequently queried but rarely updated?",
        expected_phases=["content"],
    ),

    # ── Category: multi_step (complex reasoning chains) ───────────
    TrainingPrompt(
        id="multi-001",
        category="multi_step",
        text="Design a database schema for a library management system. First analyze the requirements, then produce the SQL CREATE TABLE statements.",
        expected_phases=["thinking", "delimiter", "content"],
        notes="Extended analysis channel followed by structured output",
    ),
    TrainingPrompt(
        id="multi-002",
        category="multi_step",
        text="Debug this code and explain what's wrong:\n\ndef fib(n):\n    if n <= 1: return n\n    return fib(n-1) + fib(n-2)\n\nprint(fib(50))\n\nFirst analyze the issue, then provide the fix.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="multi-003",
        category="multi_step",
        text="Plan a REST API for a todo app. List the endpoints, then show example request/response for each.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="multi-004",
        category="multi_step",
        text="Compare merge sort and quicksort. First analyze both algorithms' properties, then give a recommendation for different scenarios.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="multi-005",
        category="multi_step",
        text="I need to deploy a Python web app. Analyze the tradeoffs between Heroku, AWS EC2, and a VPS, then recommend one for a small startup.",
        expected_phases=["thinking", "delimiter", "content"],
    ),

    # ── Category: edge_case (delimiter-confusing content) ─────────
    TrainingPrompt(
        id="edge-001",
        category="edge_case",
        text="Explain what the <|im_start|> and <|im_end|> tokens mean in the ChatML format. Be specific about their role.",
        expected_phases=["thinking", "delimiter", "content"],
        notes="Content discusses the actual delimiter tokens — tests whether the model/parser confuses content with structure",
    ),
    TrainingPrompt(
        id="edge-002",
        category="edge_case",
        text="Write a Python regex that matches strings like <|channel|> and <|message|>. Show the pattern and test cases.",
        expected_phases=["thinking", "delimiter", "content"],
        notes="Code output contains literal angle-pipe sequences",
    ),
    TrainingPrompt(
        id="edge-003",
        category="edge_case",
        text="Write an HTML template with Jinja2 tags like {{ variable }} and {% if condition %}. Include <script> tags.",
        expected_phases=["thinking", "delimiter", "content"],
        notes="Angle brackets in code content — tests false positive delimiter detection",
    ),
    TrainingPrompt(
        id="edge-004",
        category="edge_case",
        text="What does the [INST] tag do in Mistral's prompt format? How does it differ from ChatML's approach?",
        expected_phases=["thinking", "delimiter", "content"],
        notes="Content discusses Mistral's delimiters",
    ),
    TrainingPrompt(
        id="edge-005",
        category="edge_case",
        text="Write a C function that uses bitwise operators: <<, >>, |, &. Include the pipe operator in a shell command example too.",
        expected_phases=["thinking", "delimiter", "content"],
        notes="Pipe characters and angle brackets in code — maximally confusing for naive parsers",
    ),

    # ── Category: instruction_following (tests format compliance) ──
    TrainingPrompt(
        id="instr-001",
        category="instruction_following",
        text="List exactly 3 items, numbered 1-3. Each item should be one sentence about a different planet.",
        expected_phases=["content"],
    ),
    TrainingPrompt(
        id="instr-002",
        category="instruction_following",
        text="Write a haiku about programming. Format: three lines, 5-7-5 syllables. Nothing else.",
        expected_phases=["content"],
    ),
    TrainingPrompt(
        id="instr-003",
        category="instruction_following",
        text="Translate 'Hello, how are you?' into French, Spanish, and Japanese. Use this exact format:\nFR: [translation]\nES: [translation]\nJA: [translation]",
        expected_phases=["content"],
    ),
    TrainingPrompt(
        id="instr-004",
        category="instruction_following",
        text="Summarize the concept of recursion in exactly two sentences.",
        expected_phases=["content"],
    ),
    TrainingPrompt(
        id="instr-005",
        category="instruction_following",
        text="Create a markdown table with 3 columns (Language, Typing, Year) and 4 rows of programming languages.",
        expected_phases=["content"],
    ),

    # ── Category: persona (tests B4 — persona text with delimiters) ─
    TrainingPrompt(
        id="persona-001",
        category="persona",
        text="You are a QA tester for a Python web application. Describe your testing approach in 2-3 sentences. Be specific about what you would test first.",
        expected_phases=["thinking", "delimiter", "content"],
        notes="Persona description — the exact scenario where B4 manifests",
    ),
    TrainingPrompt(
        id="persona-002",
        category="persona",
        text="You are a senior backend engineer reviewing a pull request. What are the top 3 things you check? Be concise.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="persona-003",
        category="persona",
        text="Act as a database administrator. A query that used to take 100ms now takes 10 seconds. Walk through your diagnostic process.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="persona-004",
        category="persona",
        text="You are a tech lead planning a sprint. You have 5 developers and a 2-week sprint. The backlog has 20 tickets. How do you prioritize?",
        expected_phases=["thinking", "delimiter", "content"],
    ),
    TrainingPrompt(
        id="persona-005",
        category="persona",
        text="You are a security auditor reviewing a web application. List the first 5 things you would check, in order of priority.",
        expected_phases=["thinking", "delimiter", "content"],
    ),
]


def get_prompts_by_category(category: str) -> list[TrainingPrompt]:
    """Filter prompts by category."""
    return [p for p in TRAINING_PROMPTS if p.category == category]


def get_all_categories() -> list[str]:
    """Return all unique prompt categories."""
    return sorted(set(p.category for p in TRAINING_PROMPTS))


def get_prompt_by_id(prompt_id: str) -> TrainingPrompt | None:
    """Look up a prompt by its ID."""
    for p in TRAINING_PROMPTS:
        if p.id == prompt_id:
            return p
    return None
