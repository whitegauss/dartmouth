# Adapters

An adapter turns a prompt into a message. The engine builds the complete prompt; the adapter only has to deliver it and return the answer.

## Shipped adapters

### `claude_code`: Claude Code

```
claude -p --output-format json --tools <tools> --allowedTools <tools> --permission-mode dontAsk --no-session-persistence
```

- The prompt goes through **stdin**, avoiding argument-length limits.
- `--permission-mode dontAsk` denies anything not explicitly allowed. Tools are limited to read-only ones (`Read, Grep, Glob, WebSearch, WebFetch` by default) and are only enabled in the rounds listed in `tools_rounds`; in other rounds `--tools ""` is passed.
- For the chair's summary, `--json-schema` is added and the result is read from `structured_output`.
- `continue_session: true` switches to `--session-id` / `--resume` per topic. It is off by default because every turn already carries the full context, so continuing a session would send it twice.
- Records `num_turns`, the token breakdown (input / cache creation / cache read / output) and cost.

Options: `model`, `effort`, `tools`, `tools_rounds`, `cwd`, `command`, `continue_session`.

### `antigravity_cli`: Antigravity CLI (`agy`)

Check Antigravity's terms before driving `agy -p` from another program.


```
agy -p <prompt> --output-format json --mode plan --sandbox [--model …] [--effort …]
```

- `agy` does not read the prompt from stdin, so it is passed as an argument. Prompts over 100 KB are written to a private temporary file (mode 600, outside the agent's working directory) that the agent is asked to read; the file is deleted after the call, and concurrent calls never delete each other's files.
- `--mode plan` is read-only; `--sandbox` adds terminal restrictions.
- `--json-schema` is **not** used: in agy 1.5 it isn't enforced, so different keys or clarifying questions came back. The format is requested in the prompt and the engine re-asks once if it doesn't parse.
- Plan mode can make some models propose a plan or ask for confirmation instead of answering. Every prompt's general rules tell agents not to; with that rule in place, both `gemini-4-hydrogen-*` and `gemini-4-argon-*` answer directly. Hydrogen is much faster (about 15–20 s per turn versus minutes for `argon-high`), so it's the suggested default.
- `--effort max` conflicts with explicit model names like `gemini-4-argon-high`; agy rejects such pairs.

Options: `model`, `effort`, `sandbox` (default `true`), `tools_rounds`, `cwd`, `prompt_dir`, `command`.

### `gemini_api`: Gemini API (Google AI Studio key)

Calls `models/<model>:generateContent` over HTTPS with the standard library; no extra dependency.

- The key is read from `GEMINI_API_KEY` (or `api_key_env`) and sent in the `x-goog-api-key` header; it never appears in URLs, logs or the transcript.
- For the chair's summary, `responseMimeType: application/json` with a `responseSchema` enforces the format.
- HTTP 429 becomes a clear "rate limit" error (with the suggested retry delay); `dm stats` counts it separately.
- **Free tier**: Google may use unpaid-tier prompts and responses to improve its products, and humans may review them (outside the EEA, Switzerland and the UK). Give such participants `share_scope: public_only`. Free-tier daily request limits are per project and can be small; check them in AI Studio. `dm models gemini` lists the models your key can call.

Options: `model` (required), `api_key_env`, `temperature`, `thinking_budget`, `base_url`.

### `gemini_cli`: legacy Gemini CLI

The standalone Gemini CLI was retired for personal accounts in 2026 and folded into Antigravity CLI. Use `antigravity_cli` with a Gemini model instead. This adapter remains for API-key users of the old CLI:

```
gemini -p <instruction> -o json --approval-mode plan --skip-trust      # prompt via stdin
```

### `dummy`: for tests and demos

No external process. Options: `replies` (list; `{round}`, `{topic}`, `{n}`, `{name}` are substituted), `delay_s` (number or `[min, max]`), `fail_rate`, `fail_mode` (`error` or `hang`), `fail_calls`, `fail_rounds`, `bad_json`, `seed`. As chair it returns valid summary JSON.

## Writing your own adapter

```python
# my_agents/echo.py
from dartmouth.adapters.base import Adapter, Reply, TurnContext

class EchoAdapter(Adapter):
    async def respond(self, ctx: TurnContext) -> Reply:
        # ctx.prompt       full prompt (role, purpose, minutes, topic messages, instructions)
        # ctx.round        intro | independent | critique | summary | interjection
        # ctx.topic        topic number, or None
        # ctx.response_schema  JSON Schema when a structured answer is expected (chair summary)
        # self.options     every extra key from the participant's config
        return Reply(text="…", tokens=None, meta={})

    async def close(self) -> None:
        ...  # optional cleanup (browsers, connections)
```

Reference it from the config without touching the package:

```yaml
participants:
  echo:
    adapter: my_agents/echo.py:EchoAdapter      # relative to the config file
    # or, if importable: adapter: my_agents.echo:EchoAdapter
    role: member
```

Guidelines:

- **Don't swallow `asyncio.CancelledError`.** The engine cancels calls on timeout and on `/stop`; clean up and re-raise.
- For subprocesses, use `dartmouth.adapters.base.run_subprocess`: no shell, process-group kill on timeout or cancellation, logging of command, exit code and stderr (long arguments are not logged).
- For a CLI agent, subclass `CLIAdapter` (`adapters/cli_base.py`) and implement `build(ctx) -> (argv, stdin)` and `parse(rc, out, err, ctx) -> Reply`. You get the isolated working directory, `tools_rounds` handling and cleanup hooks for free.
- Raise `AdapterError` with a useful message on failure; it's shown to the user and recorded in the transcript.
- Put token counts in `Reply.tokens` and details (`turns`, `usage`, `cost_usd`) in `Reply.meta`; they're recorded per call.
