# Configuration reference

A meeting is configured by one YAML file (`config.yaml` by default; pass another with `-c`). Relative paths inside it (`context[].file`, `persona_file`, `cwd`, `prompts_dir`, `meetings_dir`, file-based adapters) are resolved **relative to the config file**, so a config and its material can live anywhere.

When a meeting starts, the resolved config is copied into the meeting directory and `dm resume` uses that copy.

## `meeting`

| Key | Default | Meaning |
|---|---|---|
| `name` | `meeting` | Shown in the minutes; also the directory slug unless `slug` is set |
| `slug` | from `name` | Directory name part: `meetings/<date>-<slug>/` |
| `purpose` | `""` | One-paragraph goal, included in every prompt |
| `topics` | `[]` | List of strings or topic objects (below). If empty, the meeting waits for `/topic` |
| `context` | `[]` | Background material included in every prompt (below) |
| `critique_rounds` | `1` | Cross-critique rounds per topic |
| `critique_parallel` | `false` | `false`: critics speak one at a time, so earlier critiques and your interjections reach later speakers. `true`: all at once (faster; interjections apply from the next round) |
| `chair_participates` | `true` | `false`: the chair skips independent answers and critiques, and only moderates and summarizes |
| `display` | `as_arrived` | `all_at_once` holds independent answers until everyone has replied (failures are still shown immediately) |
| `auto_advance` | `true` | `false`: wait for `/next` after every round |
| `agent_tools` | `true` | `false`: agents get no tools and no working directory; use with `knowledge` |
| `tools` | none | One-line switch: `off` (no tools) or `read_only`. Overrides `agent_tools` when set |
| `approval` | `human` | `auto`: approve the chair's summaries automatically; withdraw decisions later if needed |
| `extensions` | `true` | Let the chair request an extension for a topic that needs more discussion (see below) |
| `max_extensions` | `2` | Extensions per meeting |
| `intro` | `true` | Run the introduction round at the start |
| `language` | `日本語` | Language the agents are told to answer in |

### Extensions

After the last critique round, discussants may write a line starting with `【延長希望】` (extension wanted) and a reason. In its summary the chair decides whether to request an extension (`extension_requested`). A request **must** include a handover (`handover`): the best proposal so far, the open points, and the questions the extension should answer. The chair is asked once more if the handover is missing; otherwise the request is dropped.

Approved requests are queued. Extensions run **after all topics are done**, and only if the remaining budget can cover one (calls, rounds, and `max_tokens` if set). The extension topic receives the handover as its premise and none of the original topic's messages, which is why the handover is required. An extension can't request another extension. Handovers always end up in a "申し送り" (handover) section of the minutes, including ones that were skipped for lack of budget, so nothing is lost. To cancel a request, `/edit` the summary and set `extension_requested: false` before approving.

### Topic objects

```yaml
topics:
  - Plain string topic
  - title: Where should each job run?
    premise: Budget is about $10/month.    # extra conditions for this topic
    participants: [claude, gemini]         # subset for this topic; the chair always attends
    chair: gemini                          # a different chair for this topic only
```

### Context items

```yaml
context:
  - file: notes/machines.md
  - file: notes/health.md
    private: true                 # only for participants with share_scope: all
    label: Health records
  - text: Inline material works too.
```

Exactly one of `text` or `file` is required. Add `rounds: [independent, summary]` to send a large item only in those rounds instead of every turn. See [security.md](security.md) for the inline private markers.

## `budget`

| Key | Default | Meaning |
|---|---|---|
| `max_calls` | `40` | Total agent calls (including retries). When a round can't fit, the meeting jumps to the chair's summary; when nothing is left, remaining topics are skipped |
| `max_chars_per_message` | `4000` | Hard cap per message; longer replies are cut. The round prompts also give a softer target length (about 1500 chars for independent answers, 800 for critiques), because agents that write up to the cap are slow |
| `max_rounds` | `30` | Independent + critique rounds across the whole meeting |
| `claude_5h_tokens` | none | Rough Claude 5-hour allowance (tokens excluding cache reads, across all meetings in the last five hours). Over it, the chair avoids Claude backends when seating |
| `max_tokens` | none | Token allowance for the meeting. Currently used only to decide whether an extension can run (estimated from the average tokens per call so far); regular topics are not stopped by it |

Chair summaries and the closing summary always run, even over budget, so a meeting never ends without minutes.

## `participants`

A mapping from participant id to its settings. The id `user` is reserved. Exactly one enabled participant must have `role: chair`.

| Key | Default | Meaning |
|---|---|---|
| `adapter` | required | `claude_code`, `antigravity_cli`, `gemini_cli`, `dummy`, `package.module:Class`, or `path/to/file.py:Class` |
| `role` | `member` | Selects `prompts/<role>.md`. Shipped roles: `chair`, `researcher`, `engineer`, `third_opinion`, `member` |
| `display_name` | id, capitalized | Label in the log and prompts |
| `timeout_s` | `180` | Hard limit per call; the process group is killed on expiry |
| `retries` | `1` | Extra attempts after a failure or timeout |
| `on_failure` | chair: `pause`, others: `skip` | After retries are exhausted: `pause` asks you (`/retry`, `/continue`, `/abort`); `skip` marks the participant absent for that round |
| `optional` | `false` | Optional participants never pause the meeting |
| `enabled` | `true` | Disabled participants are ignored entirely |
| `share_scope` | `all` | `public_only` redacts everything marked private before it reaches this participant |
| `persona` / `persona_file` | none | Who this agent is (can / can't / good at). Used verbatim as the introduction (no model call, saves tokens) and added to every prompt. Shared personas live in `prompts/personas/` |
| `color` | palette | Label color in the TUI |

Any other key is passed to the adapter as an option; see [adapters.md](adapters.md). Common ones:

| Option | Adapters | Meaning |
|---|---|---|
| `model` | all CLI adapters | Model name or alias |
| `effort` | `claude_code`, `antigravity_cli` | Reasoning effort. For agy it must agree with the model name (agy rejects conflicting pairs) |
| `cwd` | all CLI adapters | Directory the agent may read. Default: an empty per-participant directory |
| `tools_rounds` | `claude_code`, `antigravity_cli` | Rounds where tools and `cwd` are available. Default `[independent, interjection]` |
| `tools` | `claude_code` | Allowed tools. Default `[Read, Grep, Glob, WebSearch, WebFetch]` |
| `command` | all CLI adapters | Executable to run instead of the default |

## `seating` (optional): the chair picks personas and models per topic

```yaml
meeting:
  approval: auto            # human (default): approve each summary / auto: the chair's summary is approved as is
participants:
  chair: {adapter: claude_code, model: opus, role: chair}
seating:
  personas: [engineer, researcher, critic, user_advocate, security, quota]   # files in prompts/personas/
  min_seats: 2
  max_seats: 3
  backends:                 # the engines the chair can assign; same keys as a participant
    sonnet: {adapter: claude_code, model: sonnet, note: "main workhorse"}
    haiku:  {adapter: claude_code, model: haiku, note: "lightest"}
    gemini: {adapter: gemini_api, model: gemini-3.5-flash-lite, share_scope: public_only, note: "free tier"}
```

At the start of each topic the chair receives the persona list (name, role, one-line summary from each file's front matter), the backends with their notes, whether they may see confidential material, and how each has performed in this meeting so far (calls, average seconds, failures, rate-limit hits). It returns 2–3 seats, each a persona on a backend with a one-line reason; the reasons are logged. Invalid choices are dropped and the remaining seats filled automatically.

Fixed seats sit in every topic, in addition to the ones the chair picks. Give a list of backends to rotate the model across topics and meetings (least-used first), so you can compare how each model does in that role:

```yaml
seating:
  fixed:
    - {persona: manager, backends: [agy, agy-flash, haiku]}
```

In each summary the chair scores every seat's contribution from 1 to 5 (specifics, evidence, a view no other seat gave). Scores are recorded per persona × backend; `dm stats --seats meetings/*/` and the Web UI's usage page show seats, average score and tokens per call. The chair receives this history when seating, and is told to exploit good combinations while sometimes trying under-tested ones.

The chair also sees how many Claude tokens all meetings used in the last five hours. Set `budget.claude_5h_tokens` to make it avoid Claude backends once that is exceeded. When a Claude call fails with the 5-hour limit, every Claude backend is skipped for the rest of the meeting. Backends that can't run (for example `gemini_api` without `GEMINI_API_KEY`) are left out automatically.

Confidentiality stays in your hands: a seat inherits its backend's `share_scope`, so a seat on a `public_only` backend always receives private material redacted, whatever the chair decides.

Persona files start with front matter:

```markdown
---
name: 批判役
role: third_opinion
summary: 前提を疑い、他の案の弱点を指摘する
---
(the persona text given to the seat)
```

With `approval: auto` the meeting never stops for you; review decisions afterwards on the Web UI's decisions page and withdraw the ones you don't want.

## `knowledge` (optional): materials the agents don't have to fetch

Agents that research on their own (web search, reading files) repeat tool calls and re-send the whole context each time; in measurements a single such call used about 60% of a meeting's tokens. With a knowledge base, Dartmouth finds the relevant material itself and agents only think.

```yaml
meeting:
  agent_tools: false          # give agents no tools and no working directory
knowledge:
  sources:
    - {path: ../src, glob: "**/*.py", label: src}
    - {path: ../docs, glob: "**/*.md", label: docs}
    - {path: ../meetings, glob: "*/minutes.md", label: minutes}
    - {path: ../notes, glob: "*.md", label: notes, private: true}   # never sent to share_scope: public_only
  embedder: {backend: fastembed}          # local CPU model, free; or {backend: ollama, model: bge-m3}
  # local_llm: {model: qwen2.5:3b}        # Ollama: summarizes material that exceeds max_chars
  index_dir: .dartmouth-index
  top_k: 6                # excerpts given at the start of each topic
  request_k: 3            # excerpts added per request
  requests_per_topic: 8
  max_chars: 8000         # material per turn; beyond it, the local LLM summary (if any) or the citation only
```

- Files are split into chunks with their path and line numbers, so agents can cite `path:line`. Only changed files are re-embedded.
- Search fuses vector similarity with character-bigram BM25 (exact names such as `tools_rounds` are found reliably).
- At the start of a topic, the top excerpts for its title and premise go into every turn. An agent can write `【資料請求】<query>` on its own line to add more; the result reaches the next speakers and is logged.
- `fastembed` is an optional dependency: `uv tool install --editable '.[kb]' --force` (or `uv sync --extra kb`). The default model (`paraphrase-multilingual-MiniLM-L12-v2`, about 220 MB) is downloaded once to `~/.cache/dartmouth/fastembed`.
- In critique and summary rounds the material budget is halved, and excerpts cited in the discussion or added by requests go first (everyone has already read the full set in the independent round). Repeating the same request within a topic does nothing.
- `dm kb build -c <config>` builds or updates the index; `dm kb search -c <config> "<query>"` shows what would be passed.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `prompts_dir` | `./prompts`, else the repository's `prompts/` | Where role and round templates are read from |
| `meetings_dir` | `meetings` | Where meeting directories are created |

## Prompt templates

Everything the agents are told lives in `prompts/` and can be changed without touching code:

- `<role>.md`: the stance for each role (`$display_name`, `$name` are substituted)
- `intro.md`: the introduction round (only for participants without a persona)
- `rounds/independent.md`, `rounds/critique.md`, `rounds/summary.md`, `rounds/summary_retry.md`, `rounds/final.md`, `rounds/ask.md`
- `personas/*.md`: reusable personas

Templates use `$variable` substitution (Python `string.Template`), so JSON examples with braces are safe.
