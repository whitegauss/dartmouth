# Dartmouth

Run round-based meetings between several AI coding agents (Claude Code, Gemini via Antigravity CLI, or your own adapter) and yourself, and keep the minutes automatically.

Named after the 1956 Dartmouth workshop, where the term "artificial intelligence" was coined.

- **Structured rounds per topic**: independent answers (agents can't see each other, to avoid herding), cross-critique (each agent must name at least one weakness in the others), then a chair summary that you approve, edit, or reject.
- **Extensions with a handover**: if a topic needs more discussion, the chair can request an extension with a mandatory handover note; extensions run after all topics, only if budget remains.
- **Hands-off if you like**: the chair can pick personas and models per topic from lists you provide, and approve its own summaries; you review decisions afterwards.
- **You stay in charge**: interject at any time. Your message reaches every agent from the next turn, without interrupting the one currently speaking. Only what you approve goes into the decisions.
- **Durable by design**: every message is appended (and fsync'd) to `transcript.jsonl` as it arrives. Kill the process at any point and `dm resume` picks up where it stopped.
- **Fails soft**: timeouts and crashes are isolated per agent. Optional participants are skipped; the chair failing pauses the meeting and asks you what to do.
- **Thinking only, if you want**: a local knowledge base (free CPU embeddings, optional Ollama) finds the relevant code, docs and past minutes and hands them to each turn, so agents don't spend your quota researching; they can ask for more with a one-line request.
- **Read-only agents**: agents run with read-only tools in an empty working directory and can't write files or run shell commands during a meeting.
- **Three front ends, one engine**: a Textual TUI (works over SSH at 80×24), a plain stdin/stdout mode (with a headless mode), and a Web UI reachable only from localhost and your Tailscale tailnet, where you can also start meetings from a topic you type in, or from the open questions of a previous meeting.

> **Status**: early (MVP). Agent prompts and on-screen text are in Japanese for now.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone <this repo> dartmouth && cd dartmouth
uv tool install --editable .   # puts `dartmouth` and its short alias `dm` on your PATH
```

Then log in once to each agent CLI you want to use, in its own interactive mode:

- [Claude Code](https://docs.claude.com/en/docs/claude-code): `claude`
- [Antigravity CLI](https://antigravity.google/): `agy` (also how you reach Gemini models; the standalone Gemini CLI was retired in 2026)

## Try it without any agent

```sh
dm run -c examples/dummy.yaml
```

This runs a full two-topic meeting with dummy agents (canned answers, random delays, and one flaky participant), so you can see the flow and the files it writes before spending any tokens.

## Your first real meeting

```sh
dm init                 # writes config.yaml from examples/dummy.yaml
$EDITOR config.yaml     # set the purpose, topics and participants (replace adapter: dummy with real ones)
dm run                  # TUI
dm run --ui plain       # plain stdin/stdout
dm run --ui web         # Web UI (prints URLs with an access token)
dm run --headless       # run every topic unattended; approvals are left pending
```

### Web UI: start meetings from the browser

```sh
dm web                           # lists every meeting config in ., examples/ and local/examples/
dm web --config-dir ~/my-configs # look somewhere else (repeatable); -c adds single files
```

Open the printed URL (it carries a one-time access token). The home screen is built around one job — **working through what is still unresolved**:

1. pick topics from **one list of every project's topics** (no theme / project to choose first). Each project's section shows its agenda-file topics and the chair's proposal; a meeting runs in one project, so picking a topic from another project switches to it and clears the previous picks;
2. optionally press **議長に議題を作ってもらう** in a project's header: the chair bundles the project's open questions from the ledger into a few right-sized topics (related items together, the ones that unblock the most first; items that only need a lookup are left out with a reason). The new topics come back selected, and the proposal is cached until the open items change;
3. press **start**.

Click a card to toggle it, ✎ to edit it (saved back to the file, previous version kept as `.bak`). Everything else is folded under **議題を自分で選ぶ・細かい設定**: the current project, single open items from the ledger, adding a topic to the file, one-off topics, chair model / effort, meeting name and purpose, and an answer form for questions owned by the user. Editing the agenda file on disk updates the page without a reload. To discuss a finished topic again, move it from `議論済み` back to `議題` in the agenda file.

#### The agenda file

All topics live in one YAML, grouped **theme → project → topics**. Keys can be Japanese or English:

```yaml
共通設定: {critique_rounds: 2}          # meeting options shared by every project
テーマ:
  - 名前: Home server
    マイルストーン:                        # shared milestones, shown on the ledger's timeline
      - {キー: M1, 名前: Requirements, 達成条件: requirements fixed, 期限: 2026-12}   # 2026 / 2026-12 / 2027年3月 / 2026Q2
    案件:
      - id: my-project                  # used for the ledger and the meeting directory
        名前: My project
        目的: ...
        マイルストーン期限: {M1: 2027-01}  # per-project override
        議題:
          - {題名: ..., 前提: ..., グループ: ...}
        議論済み: []                      # approved topics move here
```

Use a project from the CLI with `dm run -c agenda.yaml#my-project`. Plain config files (`meeting:` at the top level) still work and appear under "その他の設定ファイル".

#### Other pages

| Page | What it does |
|---|---|
| **Past meetings** | Minutes, full log, requirements draft and usage of any meeting; full-text search; downloads. A finished meeting can be extended with one more topic |
| **Decisions / open questions / to-dos** | Every item from every meeting in one list; mark items done; start a follow-up meeting from selected items |
| **Ledger** | Decisions, premises, unknowns (what would settle them), discussions (who decides), to-dos — across meetings, in SQLite. The **timeline** places milestone deadlines on a month/year axis (● done, ◐ in progress, ○ not started; red = overdue; vertical line = today). The **milestone table** compares projects of a theme side by side (rows) and stage by stage (columns); click a cell to filter, ⋯ to set a per-project deadline or state. Each item shows what it needs and what it unblocks, and whose **ball** it is (本人 = you, AI, 外部待ち = waiting on someone outside, 依存待ち = blocked by another item; derived from owner / kind / dependencies, or set by hand). **AI に渡す** copies (or saves) a *context pack*: one Markdown file per project, or all projects, laid out meeting → topic → decisions / open questions / to-dos with fixed fields (ID, state, ball, reason) and a by-ball index first, so an AI can read everything in one pass. Every item has a stable link `#/ledger/item/<ID>`. *Import / classify* re-reads meetings and lets the chair classify new items in batches |
| **Usage** | Calls, tokens, time and failures per agent/model and per meeting |
| **Settings** | Edit configs, prompts and personas (validated; previous version kept as `.bak`) |

Only the code for the page you open is loaded; CSS/JS are served gzip-compressed with content-hashed URLs. See [security.md](docs/security.md#web-ui-exposure) for who can reach the UI.

Each meeting gets a directory `meetings/<date>-<slug>/`:

| File | Contents |
|---|---|
| `transcript.jsonl` | Every message and event, one JSON object per line, append-only |
| `minutes.md` | Decisions (approved only), open questions, to-dos, per-topic summaries |
| `config.yaml` | The exact configuration used, so the meeting can be resumed |
| `app.log` | Commands run, exit codes, stderr (kept separate from the transcript) |
| `requirements_draft.md` | A requirements-document draft generated from the minutes at the end |

```sh
dm resume meetings/2026-11-16-requirements   # continue an interrupted meeting
dm extend meetings/2026-11-16-requirements 2 --note "focus on cost"   # reopen a finished meeting and extend topic 2
dm export meetings/2026-11-16-requirements   # regenerate minutes and the draft from the transcript
dm ledger sync                               # import decisions / premises / open questions / ToDos of all meetings into the ledger (SQLite)
dm ledger classify -c agenda.yaml#my-project              # batch: AI adds rationale, executability, SW/HW, milestone and dependencies
dm ledger status                             # where each project stands on its theme's shared milestones
dm ledger blocked / ready / brief my-project       # what can't be done and why / what can be executed now / project brief for the next meeting
dm ledger pack [my-project] [--open]          # context pack for an AI: every item with ID, state, ball (whose turn) and reason, by meeting
dm stats meetings/*/ --csv usage.csv         # calls, seconds, turns and token breakdown per agent and round
```

## During a meeting

Anything that doesn't start with `/` is an interjection.

| Command | Action |
|---|---|
| `/topic <title>` | Add a topic |
| `/next` (Ctrl+N) | Go to the next round (when `auto_advance: false`); mid-round, skip the remaining critique rounds |
| `/approve` (Ctrl+A) | Approve the chair's summary |
| `/edit` | Edit the summary, then approve. In the TUI this opens an in-app editor (Ctrl+S approve, Esc close); drafts are autosaved |
| `/reject <reason>` | Reject and redo the critique with your reason |
| `/skip <who>` / `/join <who>` | Temporarily remove / bring back a participant |
| `/ask <who> <question>` | Ask one participant only |
| `/budget` | Remaining budget and per-agent call stats |
| `/stop` | Cancel the message currently being generated |
| `/retry` / `/continue` / `/abort` | When paused after the chair fails |
| `/end` | Finish after the current topic and write the closing summary |
| `/save` / `/quit` | Save / save and quit (Ctrl+C twice also quits) |

## Documentation

- [Configuration reference](docs/configuration.md)
- [Architecture](docs/architecture.md): engine, events, the meeting state machine, resume
- [Adapters](docs/adapters.md): how each agent CLI is called, and how to write your own
- [Security and privacy](docs/security.md): read-only agents, redaction of private material, Web UI exposure

## Development

```sh
uv sync
uv run pytest
dm web --dev     # live reload: saving web_static/* reloads the browser; saving Python restarts the server
```

In `--dev` mode the server restarts itself on Python changes, keeping the access token and the Tailscale relay, and reopens the meeting that was open. If an agent is speaking, the restart waits until the meeting is idle (for example, awaiting approval), so no generation is thrown away. Prompt templates are read on every turn and need no reload.

The test suite uses dummy agents and fake binaries only; it never calls a real agent CLI or the network.

### Keeping personal data out of the repository

`dm leakscan` (or `python src/dartmouth/leakscan.py`, stdlib only) checks tracked files for home-directory paths, e-mail addresses, API keys and tokens, private keys and Tailscale addresses, plus a private term list that is never committed: `LEAKSCAN_TERMS` (one term per line, `re:` for a regex) or `leak_terms.txt` in your personal directory. Hits print only the file, line and rule number, never the matched text, so CI logs stay clean. Mark a false positive with `leakscan:ignore` on that line (at most 5 in the repo) or list files in `.leakscan-ignore`.

```sh
ln -s ../../scripts/pre-commit .git/hooks/pre-commit   # scan what you are about to commit
gh secret set LEAKSCAN_TERMS < ~/dartmouth-personal/leak_terms.txt   # strict scan in CI (.github/workflows/leakscan.yml)
```

CI runs the generic scan on every push and PR, and the strict scan on pushes to `master` and PRs from this repository; the strict job fails if the secret is missing instead of skipping.

## License

[MIT](LICENSE)
