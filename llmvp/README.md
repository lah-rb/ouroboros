# LLMvp - Local Language Model Server

A high-performance Graphql (Strawberry) server for local LLMs with static knowledge base support, request pooling, and efficient token handling.

## ✨ Features

- **GraphQL API**: Primary interaction method with subscriptions for streaming
- **OpenAI Compatibility Shim**: Optional REST endpoints for existing OpenAI integrations
- **Static Knowledge Base**: Memory-mapped token storage for large context windows
- **Llama Instance Pooling**: Concurrent request handling with warm-up on startup
- **Streaming Support**: Real-time token-by-token response streaming
- **Configuration-Driven**: YAML-based configuration system
- **Jinja Template Integration**: Flexible chat formatting with template support

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)

### Installation

```bash
# Install dependencies using uv
uv sync
```

### Configuration

1. **Set up your configuration**:
   - Download a local LLM model using huggingface, `gemma3-1b` for example
   - Create a configuration file for the model in the `./configs` directory
   - Edit `active_config.txt` in the project root
   - Add the name of your config file (`gemma3-1b`). This points to `./configs/gemma3-1b.yaml`

2. **Prepare static context** (if using a static knowledge base):
```bash
uv run llmvp.py --prep
```
This creates a static token binary at your specified location. For example `./data/gemma3-1b.tokens.bin`

3. **Start the server**:

```bash
uv run llmvp.py
# or for background operation (waits for pool ready):
uv run llmvp.py --backend
```

The `--backend` flag starts the server in the background and waits for the Llama pool to be fully warmed up before releasing the terminal:
```
lah-rb@lahrbsMacStudio llmvp % uv run llmvp.py --backend
✅ Server started in background (PID: 84271)
✅ Llama pool ready (2 instances)
lah-rb@lahrbsMacStudio llmvp %
```

## 📦 Project Structure

```
llmvp/
├── api/                  # FastAPI application and endpoints
│   ├── main.py           # Main application entry point
│   ├── graphql_api.py    # GraphQL API with Strawberry (PRIMARY)
│   └── rest_api.py       # OpenAI-compatible REST shim (optional)
├── core/                 # Core system components
│   ├── config.py         # Configuration management
│   └── models.py         # Data models (future)
├── inference/            # LLM inference logic
│   ├── llama_manager.py  # Llama instance pooling
│   ├── tokenizer.py      # Tokenization utilities
│   └── template_engine.py# Chat template rendering
├── preprocessing/        # Static context processing
│   ├── cli.py            # CLI tool for token preparation
│   └── static_tokens.py  # Memory-mapped token management
├── configs/              # Configuration files
│   └── *.yaml            # Model-specific configurations
├── tests/                # Unit Tests
│   └── test_*.py         # Function Testing Suites
├── templates/            # Jinja2 chat templates
│   └── *.jinja           # Template files
├── data/                 # Generated token files
│   └── *.tokens.bin      # Memory-mapped token buffers
|──llmvp.py               # Cli Entry
|
└── README.md             # This file
```

## 🎯 GraphQL API (Standard)

**GraphQL is the primary and recommended way to interact with LLMvp.** It provides rich querying capabilities, real-time subscriptions for streaming, and a strongly-typed schema.

The GraphQL endpoint is available at `http://localhost:8000/graphql` with an interactive Apollo Sandbox IDE.

### GraphQL Schema

**Queries:**
- `health: HealthStatus!` - Health check
- `completion(request: CompletionRequest!): CompletionResponse!` - Non-streaming completion
```bash
curl -X POST https://kippa.kipukas.us/graphql -H "Content-Type: application/json" -d '{"query": "query { health { status poolSize availableInstances } }"}'
```

**Mutations:**
- `createCompletion(request: CompletionRequest!): CompletionResponse!` - Non-streaming completion
```bash
curl -X POST http://localhost:8000 -H "Content-Type: application/json" -d '{"query": "query { completion(request: { prompt: \"Hello!\", maxTokens: 50, temperature: 0.7 }) { text tokensGenerated finished } }"}'
```

**Subscriptions:**
- `streamCompletion(request: CompletionRequest!): CompletionChunk!` - Real-time streaming tokens (requires programatic handling of websockets)

### GraphQL Schema Examples

**Health Check:**
```graphql
query {
  health {
    status
    poolSize
    availableInstances
  }
}
```

**Completion:**
```graphql
query {
  completion(request: {
    prompt: "Hello, how are you?",
    maxTokens: 100,
    temperature: 0.7
  }) {
    text
    tokensGenerated
    finished
  }
}
```

**Streaming (Subscription):**
```graphql
subscription {
  streamCompletion(request: {
    prompt: "Tell me a story",
    maxTokens: 200,
    temperature: 0.8
  }) {
    text
    isComplete
  }
}
```

## 🔌 OpenAI Compatibility Shim (Optional)

For existing services that expect OpenAI's REST API, you can enable a minimal compatibility layer by setting `app.openai_shim: true` in your configuration.

> **Note:** New integrations/capabilities should use the GraphQL API.

### Enabling the Shim

Add to your config file:
```yaml
app:
  host: "0.0.0.0"
  port: 8000
  openai_shim: true  # Enable OpenAI-compatible REST endpoints
```

When enabled, the following endpoint is available:
- `POST /v1/completions` - OpenAI-compatible completion endpoint

### REST Streaming Example

```bash
curl -X POST http://localhost:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Who are you in a nutshell?",
    "stream": true,
    "max_tokens": 50
  }'
```

## 🔧 Configuration

Configuration files are stored in the `./configs/` directory. Each model should have its own YAML configuration file.

### GraphQL Security Limits

The GraphQL API ships with configurable limits to protect against deeply nested or overly complex queries. You can tune these in your model config:

```yaml
graphql:
  max_query_depth: 10
  max_tokens: 2000
  max_aliases: 50
  introspection_enabled: true
```

If `introspection_enabled` is set to `false`, schema introspection is disabled (recommended for production).
