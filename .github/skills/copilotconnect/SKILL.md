---
name: copilotconnect
description: 'Use CopilotConnect as a local OpenAI-compatible bridge to GitHub Copilot. Use for endpoints, chat completions, streaming, tool calling, Echo/Bridge mode, and understanding GitHub Copilot or VS Code LM API constraints.'
---

# CopilotConnect

CopilotConnect exposes GitHub Copilot as a local OpenAI-compatible HTTP server.

## Quick Facts

| Item | Value |
| --- | --- |
| Base URL | `http://127.0.0.1:1288/v1` |
| Health endpoint | `http://127.0.0.1:1288/health` |
| API key | Not required |
| Bind address | `127.0.0.1` only |
| Modes | `bridge` and `echo` |

Third-party apps can usually switch to CopilotConnect by changing only the OpenAI base URL.

## Requirements

- VS Code must be running.
- GitHub Copilot must be installed and signed in.
- At least one Copilot model must be available in the current VS Code session.

If those prerequisites are missing, requests can fail with `503 upstream_unavailable_error`.

## Endpoints

### `GET /health`

```bash
curl http://127.0.0.1:1288/health
```

### `GET /v1/models`

```bash
curl http://127.0.0.1:1288/v1/models
```

### `POST /v1/chat/completions`

Supports non-streaming and streaming chat completions.

### `GET /v1/mode`

Returns the current mode: `bridge` or `echo`.

### `POST /v1/mode`

```bash
curl -X POST http://127.0.0.1:1288/v1/mode \
  -H "Content-Type: application/json" \
  -d '{"mode":"echo"}'
```

## Usage Examples

### Basic Chat With `system` + `user`

```bash
curl -X POST http://127.0.0.1:1288/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5-mini",
    "messages": [
      {"role": "system", "content": "You are a concise API assistant. Answer in JSON."},
      {"role": "user", "content": "Summarize what CopilotConnect does in one sentence."}
    ]
  }'
```

### JSON Mode

```bash
curl -X POST http://127.0.0.1:1288/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5-mini",
    "response_format": {"type": "json_object"},
    "messages": [
      {"role": "system", "content": "Return valid JSON only."},
      {"role": "user", "content": "Give me {\"product\":...,\"benefit\":...} for CopilotConnect."}
    ]
  }'
```

### Streaming

```bash
curl -N -X POST http://127.0.0.1:1288/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5-mini",
    "stream": true,
    "stream_options": {"include_usage": true},
    "messages": [
      {"role": "user", "content": "List three uses of CopilotConnect."}
    ]
  }'
```

### Tool Calling

Turn 1, request a tool call:

```bash
curl -X POST http://127.0.0.1:1288/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5-mini",
    "messages": [
      {"role": "system", "content": "Use tools when you need structured data."},
      {"role": "user", "content": "What is the weather in Tokyo?"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "Get weather for a city",
          "parameters": {
            "type": "object",
            "properties": {
              "city": {"type": "string"}
            },
            "required": ["city"]
          }
        }
      }
    ],
    "tool_choice": "auto"
  }'
```

Turn 2, send the tool result back:

```bash
curl -X POST http://127.0.0.1:1288/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5-mini",
    "messages": [
      {"role": "user", "content": "What is the weather in Tokyo?"},
      {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_weather_1",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"city\":\"Tokyo\"}"
            }
          }
        ]
      },
      {
        "role": "tool",
        "tool_call_id": "call_weather_1",
        "content": "{\"city\":\"Tokyo\",\"temperature_c\":22,\"condition\":\"Sunny\"}"
      }
    ]
  }'
```

### Python OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:1288/v1", api_key="not-needed")

response = client.chat.completions.create(
    model="gpt-5-mini",
    messages=[
        {"role": "system", "content": "You are a precise assistant."},
        {"role": "user", "content": "Explain CopilotConnect in two bullets."},
    ],
)

print(response.choices[0].message.content)
```

### Node.js OpenAI SDK

```typescript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://127.0.0.1:1288/v1",
  apiKey: "not-needed",
});

const response = await client.chat.completions.create({
  model: "gpt-5-mini",
  messages: [
    { role: "system", content: "Return compact answers." },
    { role: "user", content: "Give me a one-line summary and one caveat for CopilotConnect." },
  ],
});

console.log(response.choices[0].message.content);
```

## Parameter Support

| Parameter | Status | Notes |
| --- | --- | --- |
| `model` | Supported | Routes to the matching Copilot model when available; otherwise falls back to a Copilot default. |
| `messages` | Supported | `string` content is supported. Content arrays are supported only for text parts. |
| `stream` | Supported | SSE stream for a single choice. Streaming requests must use `n=1`. |
| `stream_options.include_usage` | Supported | Adds a final usage chunk before `[DONE]`. Token counts remain `0`. |
| `n` | Partial | Non-streaming only. `n > 1` returns multiple choices, but `stream: true` currently supports only `n=1`. |
| `response_format` | Partial | Only `{ "type": "json_object" }` is supported. `json_schema` / structured outputs are not supported. |
| `tools` | Supported | Tool-calling is supported on all models. |
| `tool_choice: "auto" | "required" | "none"` | Partial | These values are supported. |
| `tool_choice` with a specific function name | Partial | The VS Code LM API cannot strictly force a named tool. Specific-tool requests degrade to auto-selection behavior. |
| `max_tokens`, `max_completion_tokens` | Partial | Forwarded upstream, but exact behavior is model-dependent. Very small limits can still trigger upstream `no_choices` failures, especially on some models, instead of a clean `length` stop. |
| `temperature`, `top_p`, `stop` | Partial | Forwarded, but GitHub Copilot may ignore or only weakly honor them. |
| `seed`, `presence_penalty`, `frequency_penalty`, `logit_bias`, `user` | Compatibility only | Accepted for OpenAI compatibility, but not reliably honored by the VS Code LM API / GitHub Copilot. |

## Constraints And Limitations

These are important when a third-party app uses CopilotConnect in place of OpenAI.

| Constraint | Source | Effect |
| --- | --- | --- |
| Exact token usage is unavailable | VS Code LM API | `usage.prompt_tokens`, `completion_tokens`, and `total_tokens` are returned as `0`. |
| System role is not first-class | VS Code LM API | CopilotConnect folds `system` instructions into the prompt it sends upstream. |
| Exact context windows are not exposed | GitHub Copilot + VS Code LM API | CopilotConnect cannot pre-compute reliable per-model token limits. Oversized requests fail only after upstream validation. |
| Very small output limits can still fail upstream | VS Code LM API / model behavior | Some models return `503 no_choices` instead of a clean truncated completion when `max_tokens` or `max_completion_tokens` is very small. |
| Image/audio content parts are unsupported | Current bridge + VS Code LM API | `messages[].content` arrays must contain text parts only. |
| Forced named tool selection is unsupported | VS Code LM API | `tool_choice` with a specific function name cannot be enforced precisely. |
| Streaming multi-choice is unsupported | Current bridge design | `stream: true` supports only one choice per request. |

## Expected Error Cases

### `400 response_too_long`

Example response:

```json
{
  "error": {
    "message": "Response too long.",
    "type": "invalid_request_error",
    "code": "response_too_long"
  }
}
```

Meaning:

- The request exceeded what the upstream bridge can safely return.
- CopilotConnect maps this to a controlled `400` instead of letting it surface as an unhandled server error.
- Clients should treat it as a hard limit, not as a retryable capacity condition.

### `503 upstream_capacity_error` / `no_choices`

Example response:

```json
{
  "error": {
    "message": "Response contained no choices.",
    "type": "upstream_capacity_error",
    "code": "no_choices"
  }
}
```

Meaning:

- This is usually an upstream GitHub Copilot / VS Code LM API capacity or throttling condition.
- It is not usually a CopilotConnect logic bug.
- CopilotConnect retries this condition automatically before returning the final error.
- Clients should retry with backoff.
- Very small `max_tokens` or `max_completion_tokens` values can also trigger this upstream behavior.

### `400 invalid_request_error` / `context_length_exceeded`

Example response:

```json
{
  "error": {
    "message": "Message exceeds token limit.",
    "type": "invalid_request_error",
    "code": "context_length_exceeded",
    "param": "messages"
  }
}
```

Meaning:

- This is an upstream GitHub Copilot model limit, not a CopilotConnect server bug.
- Reduce prompt size, conversation history, or retrieved context.
- Exact per-model limits are not published through the VS Code LM API.

### `503 upstream_unavailable_error` / `no_model_available`

This usually means VS Code has no accessible Copilot model in the current session.

## Key Compatibility Rule

The most important practical rule is: for streaming requests, keep `n=1`. If you need multiple candidates, use non-streaming requests with `n > 1` and handle model-dependent limits and `503 no_choices` behavior.

## Echo Mode

Echo mode is for integration testing without consuming real Copilot requests.

- `POST /v1/mode` with `{"mode":"echo"}` enables it.
- Chat responses become `[Echo] <last user message>`.
- Streaming shape, JSON shape, and `/v1/models` shape remain OpenAI-compatible.

## Integration Advice

- Treat CopilotConnect as a local compatibility bridge, not as a perfect OpenAI clone.
- Retry `503 upstream_capacity_error` with exponential backoff.
- Keep prompts smaller than you would for providers with explicit token accounting, because exact upstream context windows are opaque.
- Prefer text-only chat payloads unless you add your own pre-processing before calling CopilotConnect.