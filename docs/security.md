# Security and privacy

Dartmouth hands your material to third-party AI services. This page describes what it does to limit what agents can do and what they get to see, and where those limits end.

## Agents are read-only during a meeting

| Adapter | How |
|---|---|
| `claude_code` | Only read-only tools are allowed (`Read, Grep, Glob, WebSearch, WebFetch`) with `--permission-mode dontAsk`, which denies anything else without prompting. Tools are disabled entirely outside `tools_rounds` |
| `antigravity_cli` | `--mode plan` (read-only) and `--sandbox` |
| `gemini_cli` | `--approval-mode plan`; never `--yolo` |

All CLI adapters:

- run without a shell (`asyncio.create_subprocess_exec`), so prompt contents can't inject commands;
- run in an **empty per-participant working directory** by default. This keeps agents away from the meeting directory, where `transcript.jsonl` holds other participants' answers (which would break the independent round) and unredacted private material. Point `cwd` at a directory only when the agents should read it, and only in rounds listed in `tools_rounds`;
- are killed with their whole process group on timeout or cancellation.

These are the agents' own permission systems. Dartmouth configures them conservatively but can't make guarantees beyond what each CLI enforces.

## Marking material as private

Material can be marked private in three ways:

| Marker | Example |
|---|---|
| Inline tag (unclosed runs to the end) | `<private>blood pressure 135/88</private>` |
| HTML comment pair (renders cleanly in Markdown) | `<!-- private -->` … `<!-- /private -->` |
| Line marker, anywhere in a line, up to the end of the line | `[private] account 1234` (also `[非公開]`) |
| Whole context file | `context: [{file: notes/health.md, private: true}]` |

For participants with `share_scope: public_only`, every piece of text that goes into their prompt is redacted: the purpose, background material, minutes, topic titles and premises, other participants' messages, your interjections, rejection reasons and `/ask` questions. Private context files are omitted entirely. This is covered by tests.

**Limits:**

- Participants with `share_scope: all` see private material. They're instructed to wrap anything they repeat from it in `<private>` so it stays redacted downstream, but a model can forget to.
- Redaction is pattern-based. Unmarked sensitive text is sent as is.
- Everything is written unredacted to the local meeting directory. `.gitignore` excludes `meetings/` by default; keep it that way if you version your configs.

## Credentials

Dartmouth never handles API keys or tokens for the agents: each CLI uses its own login. Commands are logged to `app.log` without long arguments (prompts), and environment variables are not logged.

## Web UI exposure

`dm run --ui web` listens only on:

- `127.0.0.1`, and
- your Tailscale address, when `tailscale` runs on the same Linux host.

It refuses `0.0.0.0` and `::`. On WSL, where Tailscale usually runs on the Windows side and its address can't be bound from inside WSL, Dartmouth asks Windows' `tailscale serve --http <port>` to relay to `127.0.0.1:<port>`. That relay is reachable only inside your tailnet. It's created at startup and removed on exit, and an existing serve configuration on the same port is never touched. The relay uses HTTP, because HTTPS certificates are opt-in per tailnet; traffic is still encrypted by WireGuard between your devices.

Access requires a random token generated at each start and printed in the console. The first request with `?token=` sets an `HttpOnly`, `SameSite=Strict` cookie and redirects to a URL without the token, so the token doesn't stay in browser history or screenshots. There are no user accounts: anyone with the URL and token, inside your tailnet, has full control of the meeting.

### Editing settings from the browser

The settings page can write only the config files passed with `dm web -c` and the `*.md` files under their prompt directories. A config is validated before it replaces the file, and the previous version is kept as `<name>.bak`. Because a config can name code to run, two kinds of change are refused from the browser and must be made in the file directly:

- adding an adapter that isn't built in (`package.module:Class` or `path/to/file.py:Class`) that the file didn't already use;
- changing a participant's `command`.

Item statuses (to-do done, question resolved) are stored per meeting in `item_status.json`; `transcript.jsonl` is never rewritten.

## Reporting a vulnerability

Please open a private security advisory on the repository instead of a public issue.
