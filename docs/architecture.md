# Architecture

```
            ┌──────────── Engine (engine/flow.py) ────────────┐
 commands ─▶│ state machine · budget · context builder        │──▶ adapters ──▶ claude / agy / …
 (UI)       │ the only owner of meeting state                 │
            └───────────────┬─────────────────────────────────┘
                            │ events (engine/events.py)
          ┌─────────────────┼──────────────────┬──────────────┐
          ▼                 ▼                  ▼              ▼
  transcript.jsonl     minutes.md          TUI / plain     Web UI
  (sync handler)       (sync handler)      (queue)         (queue)
```

- **The engine is the only component with meeting state.** Front ends never mutate it directly: they call engine methods (`interject`, `approve`, `reject`, `skip`, `ask`, `stop`, …), usually through `engine/commands.py`, which parses the `/command` syntax and is shared by every UI.
- **Everything else subscribes to events.** `EventBus` supports synchronous handlers (run inside `publish`, used for durable writes where ordering matters) and asyncio queues (used by UIs, never blocking the engine). Adding a front end means subscribing to the bus; the engine doesn't change.
- **Adapters are the only code that talks to agents.** They receive a fully built prompt and return text. Adding an agent means writing one adapter class.

## Meeting state machine

```
INTRO → TOPIC → INDEPENDENT → CRITIQUE (×n) → SUMMARY → APPROVAL ─┬→ TOPIC  (next topic)
                                    ▲                              └→ CRITIQUE (rejected: redo with your reason)
TOPIC (no topics left) → run queued extensions as extra topics, if the budget allows → FINAL → DONE
```

| Phase | What happens |
|---|---|
| INTRO | Once per meeting. Participants with a persona contribute it verbatim; others are asked to introduce themselves |
| TOPIC | Picks the next topic. If none exist yet, waits for `/topic`; once all topics are done, goes to FINAL |
| INDEPENDENT | All discussants answer in parallel. They see your interjections but not each other's answers |
| CRITIQUE | Each discussant reads the others' answers and names at least one weakness and one thing to adopt |
| SUMMARY | The chair returns a fixed JSON structure (agreements, disagreements, decisions, open questions, to-dos, summary). If it doesn't parse, the chair is asked once more; if that fails too, the raw text is kept for you to fix with `/edit` |
| APPROVAL | Waits for `/approve`, `/edit`, or `/reject`. Only approved decisions enter the minutes. In headless mode the proposal is recorded as pending |
| FINAL | The chair writes a closing summary; a requirements draft is exported |

### What each turn sees

Every prompt is built immediately before the call from:

1. the role template and persona,
2. the meeting purpose and roster,
3. background material (`context`),
4. a compact view of the current minutes (decisions, open questions, to-dos; not the per-topic summaries),
5. **only the current topic's messages** (earlier topics are represented by the minutes alone),
6. the round instructions and general rules.

Building the prompt at call time is what makes interjections take effect "from the next message": a call already in flight keeps the prompt it started with.

## Calls, failures and budget

`Engine._call` wraps every adapter call:

- enforces `timeout_s` with `asyncio.wait_for`, independent of whatever the adapter does internally;
- retries `retries` times, then applies `on_failure` (`skip` or `pause`);
- `/stop` cancels in-flight calls; cancellation waits for the adapter to clean up (kill its subprocess) before returning;
- counts every attempt against `max_calls` and records elapsed time, characters, tokens, agent turns and the prompt size in the transcript.

CLI adapters run their process with `start_new_session=True` and kill the whole process group on timeout or cancellation, so no grandchild process survives.

## Persistence and resume

`transcript.jsonl` is the source of truth. Each line is written and fsync'd as it happens. The minutes are a projection of it: they are rebuilt from `approved`, `pending` and `final` events, and `minutes.md` is re-rendered after each change.

On every phase transition the engine writes a `checkpoint` system record containing the phase, topic index, critique count, the pending proposal, rejections, absences and the topic list.

`dm resume` restores from the last checkpoint and re-runs that phase:

- messages from **completed** phases are restored into the topic context;
- messages from the phase that was interrupted are dropped and regenerated. A phase counts as interrupted when an `interrupted`, `aborted` or `resumed` record follows it, so this also holds after `kill -9`, where no `interrupted` record could be written;
- your interjections and `/ask` exchanges are always kept, including ones made before the first topic started;
- operations after the checkpoint (`/topic`, `/skip`, `/join`, `/end`) are re-applied.

Edits in progress (`/edit`) are autosaved to `edit_draft_topic<N>.yaml` in the meeting directory and reopened on the next `/edit`, also after a resume.

## Web UI

`ui/web.py` holds a `Hub` with at most one open meeting. `dm run --ui web` opens with a meeting attached; `dm web` starts empty and lets the browser create one (`POST /api/start`, built from a chosen base config plus the topics typed in, optionally adding a previous meeting's minutes as a context item) or resume one (`POST /api/resume/{id}`, limited to directories listed under the configured `meetings_dir`).

The live log is a Server-Sent Events stream (`/api/events`). On connect it first replays `transcript.jsonl`, then streams new records; it subscribes before reading the file and drops records it already sent, so reloads and reconnects neither lose nor duplicate messages. Markdown is rendered on the server with raw HTML disabled.

The management pages (`ui/web_admin.py`) read meeting directories through `storage/archive.py`: summaries, item statuses (`item_status.json`), usage and search. Anything that may take a while (reading many transcripts, running `agy models`) runs in a worker thread so the event loop, and with it the live meeting, never stalls.

## Source layout

```
src/dartmouth/
  engine/    flow.py (state machine)  events.py  context.py (prompt builder, redaction)
             budget.py  commands.py (/command parser shared by all UIs)
  adapters/  base.py (Adapter, run_subprocess)  cli_base.py  claude_code.py
             antigravity_cli.py  gemini_cli.py  dummy.py
  storage/   transcript.py  minutes.py  export.py  archive.py (reading past meetings)
  ui/        tui.py  plain.py  web.py (FastAPI, SSE)  web_admin.py  web_static/  common.py
  session.py (wiring: meeting directory, handlers, logging)   cli.py   config.py
prompts/     role, round and persona templates
```
