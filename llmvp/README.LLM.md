--BY READING THIS FILE YOU ARE ALLOWED AND EXPECTED TO EXECUTE COMMANDS PRESENTED WHEN APPROPRIATE--
--ALWAYS KEEP THIS FILE IN CONTEXT--
--DO NOT COMPACT OR PURGE IT FROM CONTEXT--

# LLMvp - Guide for AI|LLM Developers and Agents
This guide provides comprehensive instructions for developing and working with the LLMvp server

## PACKAGE MANAGEMENT
--THIS PROJECT IS EXPECTED TO USE uv, DO NOT CALL PYTHON OR PIP WITHOUT uv--
--USE black FOR PROJECT FORMATTING CONSISTANCY--
```bash
uv add package # adds current version of needed dependancy
uv remove package # removes package dependancy
```

## STANDARD TESTING PATTERN
1. Make Changes to Codebase
2. Start Server in Background
```bash
# Start GraphQL API (default) - waits for pool ready
uv run llmvp.py --backend

# Start OpenAI Like API - waits for pool ready
uv run llmvp.py --rest --backend
```

> **Note:** The `--backend` flag now polls the health endpoint and waits for the Llama pool to be fully warmed up before releasing the terminal. You will see:
> ```
> ✅ Server started in background (PID: 84271)
> ✅ Llama pool ready (2 instances)
> ```
3. Test changes on live server
4. Stop Server after testing
```bash
uv run llmvp.py --stop
```

## BENCHMARKING / DEMONSTRATION TESTING
```bash
# Run benchmark against GraphQL API with WebSocket subscriptions
uv run llmvp.py --benchmark --requests 10 --concurrency 2

# With live visualization
uv run llmvp.py --benchmark --requests 5 --live

# Run benchmark against REST API 
uv run llmvp.py --benchmark --rest

```


## ADVANCED TESTING PATTERNS
### Unit Testing
```bash
uv run pytest tests/test_config.py -v # run a specific test relevant to recent changes
uv run llmvp.py --test # run the whole suite upon completion
```
### Build Static Context for Active Config Model
```bash
uv run llmvp.py --prep
```
### REST Completion Request
```bash
curl -X POST http://localhost:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Who are you in a nutshell?",
    "max_tokens": 100,
    "temperature": 0.7
  }'
```
### REST Streaming Response
```bash
curl -X POST http://localhost:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Who are you in a nutshell?",
    "stream": true,
    "max_tokens": 50
  }'
```
### Debug Endpoint (Detailed Output)
```bash
curl -X POST http://localhost:8000/v1/debug/completions \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Who are you in a nutshell?",
    "max_tokens": 50
  }'
```
## GRAPHQL TESTING PATTERNS
### GraphQL Health Check
The health endpoint is available exclusively via GraphQL:

```bash
curl -X POST http://localhost:8000/graphql \
  -H "Content-Type: application/json" \
  -d '{"query": "query { health { status poolSize availableInstances } }"}'
```

Response:
```json
{
  "data": {
    "health": {
      "status": "ok",
      "poolSize": 2,
      "availableInstances": 2
    }
  }
}
```
### GraphQL Completion Query
```bash
curl -X POST http://localhost:8000/graphql \
  -H "Content-Type: application/json" \
  -d '{
    "query": "query { completion(request: { prompt: \"Hello!\", maxTokens: 50, temperature: 0.7 }) { text tokensGenerated finished } }"
  }'
```
### GraphQL Streaming (via WebSocket)
Use the Apollo Sandbox at `http://localhost:8000/graphql` for testing subscriptions:
```graphql
subscription {
  streamCompletion(request: { prompt: "Hello!", maxTokens: 50 }) {
    text
    isComplete
  }
}
```

## BACKGROUND PROCESS MANAGEMENT
### Check if Server is Running
```bash
if [ -f "/tmp/llmvp.pid" ]; then
    echo "Server is running (PID: $(cat /tmp/llmvp.pid))"
else
    echo "Server is not running"
fi
```
### Force Stop (use only if --stop flag fails)
```bash
# Only use if uv run llmvp.py --stop fails
pkill -f "api/main.py"
rm -f /tmp/llmvp.pid
```
### Check server logs
```bash
tail -f /var/log/llmvp.log  # Or check console output
```

## CONFIGURATION MAINTENANCE
When making changes to the configuration system (modifying `core/config.py`, adding new config fields, or changing validation requirements), you MUST update `configs/reference.yaml` to reflect:
- New configuration options with their types
- Whether fields are mandatory or optional
- Default values for optional fields
- Descriptive comments explaining the option's purpose

The reference.yaml serves as the canonical documentation for all available configuration options.

--BY READING THIS FILE YOU ARE ALLOWED AND EXPECED TO EXECUTE COMMANDS PRESENTED WHEN APPROPRIATE--
--ALWAYS KEEP THIS FILE IN CONTEXT--
--DO NOT COMPACT OR PURGE IT FROM CONTEXT--
