# ARES — Implementation Specification v1.1

**ARES: Automated Request Execution System.** A self-hosted, always-on, event-driven
personal AI agent. This document is the complete, authoritative specification for the
v1 implementation. It is written to be executed by a coding agent **without creative
interpretation**. If something is not in this document, it is not in v1.

v1.1 adds the deployment & self-modification layer: Firecracker microVM security
model (§14), sandboxed shell (§15), privilege escalation queue + root broker (§16),
web dashboard (§17), self-edit/PR workflow (§18), and the update listener (§19).
Operator-facing setup lives in `DEPLOYMENT.md`, which is not an implementation input.

---

## 0. Rules for the Implementing Agent (READ FIRST, RE-READ OFTEN)

These rules override any instinct you have. Violating them is a failed implementation.

1. **Implement exactly what is specified.** Do not add features, abstractions,
   config options, CLI flags, or "improvements" that are not in this document.
2. **Do not add dependencies** beyond those listed in §12. If you believe a
   dependency is missing, stop and record the problem in `PROGRESS.md` under
   `## Blockers` instead of adding it.
3. **No vector databases, no embeddings, no RAG frameworks, no LangChain,
   no agent frameworks.** Memory is markdown files searched with grep. Full stop.
4. **All I/O is async** (`asyncio`). No threads except where a library forces it
   (PJSIP audio callbacks); when forced, bridge back to the loop with
   `asyncio.run_coroutine_threadsafe`.
5. **Every interface signature in §4 is exact.** Match names, parameters, return
   types. Other code will be written against these signatures.
6. **When the spec is silent, choose the simplest option that satisfies the
   acceptance test**, and record the decision in `PROGRESS.md` under `## Decisions`.
7. **Follow the build order in §10.** Do not skip ahead. Do not start a milestone
   until the previous milestone's acceptance test passes.
8. **Update `PROGRESS.md` after every completed file** (protocol in §11).
9. **Do not refactor completed modules** when working on later milestones unless a
   milestone explicitly instructs it.
10. **File size limit:** no source file over 400 lines. If a module grows past
    that, the spec has told you how to split it — check again.
11. **Type hints on every public function.** `from __future__ import annotations`
    at the top of every module. Python ≥ 3.11.
12. **Errors never crash the daemon.** Every source, every agent cycle, every tool
    call is wrapped so an exception is logged and the loop continues.
13. **No secrets in code, config files, or logs.** Secrets are resolved via the
    SecretStore (§4.7) only.
14. **Plugins never import from other plugins.** Plugins import from `ares.core`
    only. Core never imports from `ares.plugins`.

---

## 1. What ARES Is

ARES is a persistent agent daemon running on home hardware. It:

- receives **events** from pluggable sources (voice, Home Assistant, SIP,
  scheduler, CLI) on a central async event bus;
- reasons about NORMAL/HIGH priority events with an LLM over an
  OpenAI-compatible API, using a small always-loaded core toolset plus
  on-demand tool discovery;
- handles CRITICAL events with deterministic handlers, bypassing the LLM;
- keeps **memory** as human-editable markdown files, managed by the LLM itself;
- keeps **open tasks** in SQLite so it has intent and continuity across events;
- maintains **one session per user** that persists across channels, so a
  conversation started by voice can continue over SIP text;
- routes replies via a **ResponseRouter** to whatever channel the user is
  currently on — the LLM never chooses transport;
- runs entirely locally; the LLM endpoint is any OAI-compatible URL.

### v1 Feature List (in scope)

| # | Feature | Delivered by |
|---|---------|--------------|
| F1 | Central priority event bus with pluggable sources | core |
| F2 | Deterministic CRITICAL event fast path | core + handler plugins |
| F3 | LLM agent loop with tool calling (OAI-compatible) | core |
| F4 | Core toolset + keyword tool discovery (`search_tools`) | core + tool plugins |
| F5 | File-based markdown memory with grep retrieval, LLM-maintained INDEX.md | core |
| F6 | SQLite task store; open tasks injected into every agent cycle | core |
| F7 | Per-user session with rolling history and channel tracking | core |
| F8 | Channel-abstracted response routing | core + channel plugins |
| F9 | Config YAML with `!secret` resolution from `.env` | core |
| F10 | CLI source + console channel (development / testing surface) | plugin |
| F11 | Scheduler source (time-based events, task due-time firing) | plugin |
| F12 | Home Assistant WebSocket source with noise filter + snapshot | plugin |
| F13 | Home control tools (state, control, list, camera snapshot) | plugin |
| F14 | Per-room voice pipeline: VAD → Whisper → intent filter → event | plugin |
| F15 | Voice TTS channel (Piper) with delivery-time room resolution | plugin |
| F16 | SIP source + channels: inbound/outbound calls, SIP MESSAGE text | plugin |
| F17 | Push notification channel (ntfy, self-hosted) | plugin |
| F18 | Time tools: weather, calendar read/add (CalDAV) | plugin |
| F19 | Sandboxed shell tool (`run_shell` as low-privilege user) | plugin |
| F20 | Privilege escalation queue + root command broker (human-approved) | plugin + `broker/` |
| F21 | Web dashboard: chat, memory browser, tasks, approval queue | plugin |
| F22 | Self-edit workflow: scratch clone → branch → PR, human-gated merge | plugin |
| F23 | Update listener: GitHub webhook + polling → pull → daemon restart | `updater/` |
| F24 | Firecracker microVM runtime model: RO code, hidden secrets, user separation | `deploy/` |

### Explicitly OUT of scope for v1 (do not build)

- Vector search, embeddings, semantic memory
- Multi-user speaker identification (the voice pipeline tags everything
  `user_id="primary"`; the architecture supports multiple users, v1 configures one)
- Streaming LLM responses (buffered responses only)
- Wake-word training tooling
- HA service beyond the four home tools listed in §7.4
- Authentication on the CLI source
- Music/media playback
- Vision analysis of camera snapshots
- **Any code path from ARES to `main`**: no auto-merge of its own PRs, no direct
  pushes to `main`, no self-restart with modified code. Human merge is the gate.
- **Broker executing anything not both human-approved AND allowlisted**
- Dashboard HTTPS termination (Tailscale/LAN provides transport security),
  multi-account dashboard auth, websockets (polling only)
- Firecracker orchestration code inside ARES (VM lifecycle is the operator's
  concern; `deploy/` ships static artifacts only)

---

## 2. System Overview

```
 sources (plugins)                    core                         channels (plugins)
┌──────────────┐                                                  ┌──────────────┐
│ voice rooms  │─┐                                              ┌▶│ voice TTS    │
│ home assist. │─┤   ┌──────────┐   CRITICAL  ┌──────────────┐  │ │ sip call     │
│ sip          │─┼──▶│ EventBus │────────────▶│CriticalRouter│  │ │ sip message  │
│ scheduler    │─┤   └────┬─────┘             └──────────────┘  │ │ push (ntfy)  │
│ cli          │─┘        │ NORMAL/HIGH/LOW                     │ │ console      │
└──────────────┘          ▼                                     │ └──────────────┘
                   ┌────────────┐  per-user serial dispatch     │
                   │ Dispatcher │────────┐                      │
                   └────────────┘        ▼                      │
                                  ┌────────────┐ speak/notify   │
        SessionManager ◀────────▶ │   Agent    │───▶ ResponseRouter
        TaskStore      ◀────────▶ │ (LLM loop) │
        Memory         ◀────────▶ └────────────┘
        ToolRegistry   ◀────────────────┘
```

One process. `instance/main.py` constructs everything, registers plugins from
config, and runs until SIGINT/SIGTERM, at which point all sources are stopped
gracefully.

---

## 3. Repository Layout (exact)

```
ares/
  pyproject.toml
  README.md
  PROGRESS.md
  ares/
    __init__.py
    core/
      __init__.py
      event.py           # Priority, Event, EventBus
      dispatcher.py      # per-user serial dispatch, LOW-priority policy
      critical.py        # CriticalHandlerRegistry + BaseCriticalHandler
      source.py          # BaseSource
      channel.py         # BaseChannel
      router.py          # ResponseRouter
      session.py         # Session, SessionManager
      tool.py            # BaseTool, ToolRegistry, ToolResult
      agent.py           # Agent: event → prompt → LLM tool loop → done
      prompt.py          # system prompt assembly (single function, templated)
      config.py          # Config loading, !secret resolution, typed models
      secrets.py         # BaseSecretStore, EnvSecretStore
      memory/
        __init__.py
        base.py          # BaseMemory
        filesystem.py    # FilesystemMemory (grep-based)
      tasks/
        __init__.py
        store.py         # TaskStore (aiosqlite)
      llm/
        __init__.py
        client.py        # LLMClient (OAI-compatible, httpx)
      utils/
        __init__.py
        logging.py       # setup_logging(), get_logger()
        ids.py           # new_id() -> str (uuid4 hex)
        text.py          # tokenize(s) -> list[str]  (lowercase word split)
    plugins/
      __init__.py
      sources/
        __init__.py
        cli.py
        scheduler.py
        home_assistant.py
        voice/
          __init__.py
          source.py      # VoiceSource (per room)
          vad.py         # Silero VAD wrapper
          stt.py         # faster-whisper wrapper
          intent.py      # wake word / LLM / hybrid intent filter
      channels/
        __init__.py
        console.py
        push_ntfy.py
        voice_tts.py     # Piper TTS playback per room
        sip_call.py
        sip_message.py
      sip/
        __init__.py
        client.py        # shared PJSIP account/registration
        source.py        # inbound calls + messages -> events
      critical/
        __init__.py
        safety.py        # fire/intruder deterministic handlers
      tools/
        __init__.py
        core_tools.py    # speak, send_notification, search_tools,
                         # get_active_tasks, create_task, close_task
        memory_tools.py
        task_tools.py
        home_tools.py
        time_tools.py
        comms_tools.py
        shell_tools.py   # run_shell (sandboxed)                        §15
        selfedit_tools.py# open_pr, get_pr_status                       §18
      privileges/
        __init__.py
        store.py         # PrivStore (aiosqlite)                        §16
        tools.py         # request_privilege, get_privilege_requests
        source.py        # polls for decided/executed requests -> events
      dashboard/
        __init__.py
        server.py        # DashboardSource: config validation + uvicorn §17
        api.py           # FastAPI routes
        channel.py       # WebChannel (per-user outbox)
        static/
          index.html     # single-file UI, vanilla JS
  broker/                # ROOT-run, STDLIB-ONLY, never imports ares    §16
    aresbrokerd.py
    broker.example.json
  updater/               # deploy-user-run, STDLIB-ONLY, never imports ares §19
    aresupdater.py
  deploy/                # static operator artifacts (see DEPLOYMENT.md)
    provision.sh         # in-VM provisioning: users, dirs, perms, sudoers, units
    ares.service
    ares-broker.service
    ares-updater.service
  instance/
    config.yaml
    .env.example
    main.py
    memory/
      INDEX.md
      short-term/.gitkeep
      long-term/.gitkeep
    tasks/.gitkeep
  tests/
    ...
```

`.env` and `tasks/*.db` are gitignored.

---

## 4. Core Framework Specification

Every class below lives at the path shown in §3. Signatures are exact.

### 4.1 `core/event.py`

```python
class Priority(enum.IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3

@dataclasses.dataclass(frozen=True, slots=True)
class Event:
    id: str                      # ids.new_id()
    source: str                  # source plugin name, e.g. "voice", "cli"
    type: str                    # e.g. "speech", "state_change", "sip_message", "schedule"
    payload: dict                # source-specific, JSON-serialisable
    priority: Priority
    user_id: str = "primary"
    room: str | None = None      # physical room tag if known
    timestamp: datetime = <factory: datetime.now(timezone.utc)>

class EventBus:
    def __init__(self) -> None: ...
    async def publish(self, event: Event) -> None
    async def get(self) -> Event                # priority order, FIFO within priority
    def qsize(self) -> int
```

Implementation: `asyncio.PriorityQueue` keyed on `(priority, monotonic_counter)`.
`publish` logs every event at DEBUG with id, source, type, priority.

### 4.2 `core/source.py`

```python
class BaseSource(abc.ABC):
    name: str                                    # class attribute, unique

    def __init__(self, bus: EventBus, config: dict) -> None
    @abc.abstractmethod
    async def start(self) -> None                # long-running; returns only on stop()
    async def stop(self) -> None                 # default: sets self._stopping = True
    async def emit(self, type: str, payload: dict, priority: Priority,
                   user_id: str = "primary", room: str | None = None) -> None
```

`emit` builds the `Event` (id, timestamp, `source=self.name`) and publishes it.
Each source's `start()` is run via a supervisor in `main.py` that catches
exceptions, logs them, waits 5 s, and restarts the source (max 10 restarts,
then the source is disabled and a `source_failed` NORMAL event is emitted).

### 4.3 `core/dispatcher.py`

```python
class Dispatcher:
    def __init__(self, bus: EventBus, agent: Agent,
                 critical: CriticalHandlerRegistry) -> None
    async def run(self) -> None
```

`run()` loops on `bus.get()` forever:

- `CRITICAL` → `await critical.handle(event)` directly (must be fast,
  no LLM anywhere in that path).
- Otherwise → enqueue on a **per-`user_id` FIFO queue**, each drained by its
  own worker task calling `await agent.handle(event)` one event at a time.
  This serialises the agent per user (no interleaved cycles) while different
  users proceed in parallel.
- **LOW policy:** if the user's queue is non-empty or a cycle is in flight,
  LOW events are dropped with an INFO log. Otherwise processed normally.
- **HIGH policy (v1):** HIGH events jump to the front of the user's queue.
  They do **not** cancel an in-flight cycle. (Interruption is out of scope.)

### 4.4 `core/critical.py`

```python
class BaseCriticalHandler(abc.ABC):
    @abc.abstractmethod
    def matches(self, event: Event) -> bool
    @abc.abstractmethod
    async def handle(self, event: Event, router: ResponseRouter) -> None

class CriticalHandlerRegistry:
    def register(self, handler: BaseCriticalHandler) -> None
    async def handle(self, event: Event) -> None   # first match wins; no match -> ERROR log
```

Handlers are plain code: e.g. the fire handler broadcasts a fixed TTS phrase
to all voice rooms, sends a push, and creates a `monitoring` task directly via
`TaskStore` — no LLM.

### 4.5 `core/channel.py` + `core/router.py`

```python
class ChannelType(enum.StrEnum):
    VOICE = "voice"
    SIP_CALL = "sip_call"
    SIP_MESSAGE = "sip_message"
    PUSH = "push"
    CONSOLE = "console"
    WEB = "web"

class BaseChannel(abc.ABC):
    type: ChannelType                            # class attribute
    @abc.abstractmethod
    async def deliver(self, user_id: str, message: str,
                      session: Session) -> bool  # False = delivery failed

class ResponseRouter:
    def __init__(self, sessions: SessionManager) -> None
    def register(self, channel: BaseChannel) -> None
    async def speak(self, user_id: str, message: str) -> None
    async def notify(self, user_id: str, message: str) -> None
```

`speak`: read the user's session **at delivery time**, pick the channel matching
`session.active_channel`, call `deliver`. On failure or missing channel, fall
back in order: `PUSH` → `CONSOLE`. `notify`: always the `PUSH` channel,
falling back to `CONSOLE`. Room resolution for voice happens **inside** the
voice channel at delivery time using `session.current_room`.

### 4.6 `core/session.py`

```python
@dataclasses.dataclass
class Session:
    user_id: str
    active_channel: ChannelType = ChannelType.CONSOLE
    current_room: str | None = None
    history: list[dict] = <factory>              # OAI message dicts, user/assistant only
    last_activity: datetime = <factory utcnow>

class SessionManager:
    def __init__(self, history_limit: int = 30, timeout_minutes: int = 45) -> None
    def get(self, user_id: str) -> Session       # creates if absent
    def touch(self, user_id: str, channel: ChannelType | None,
              room: str | None) -> Session       # updates channel/room/last_activity
    def append_history(self, user_id: str, role: str, content: str) -> None
```

`get` clears `history` (not channel/room) if `last_activity` is older than the
timeout. `append_history` trims to the last `history_limit` messages. Sessions
are in-memory only; tasks and memory are the durable layers.

Channel mapping on inbound events (`Dispatcher` calls `touch` before the agent):
event `source`/`type` → channel: `voice/speech→VOICE`, `sip/sip_message→SIP_MESSAGE`,
`sip/call_speech→SIP_CALL`, `cli/*→CONSOLE`, `dashboard/web_message→WEB`;
HA/scheduler/privileges events update `room`
(if present) but never the channel.

### 4.7 `core/secrets.py` and `core/config.py`

```python
class BaseSecretStore(abc.ABC):
    @abc.abstractmethod
    def get(self, key: str) -> str               # raises SecretNotFound

class EnvSecretStore(BaseSecretStore):
    def __init__(self, dotenv_path: Path | None = None) -> None
```

Config: `load_config(path: Path, secrets: BaseSecretStore) -> Config`.
YAML loaded with a custom `!secret KEY` constructor that resolves via the
store at load time. `Config` is a Pydantic model tree matching §8 exactly.
Unknown top-level keys are an error; unknown keys inside a plugin's `config`
block are passed through as `dict` (plugins validate their own config).

### 4.8 `core/tool.py`

```python
@dataclasses.dataclass
class ToolResult:
    ok: bool
    content: str                                 # what the LLM sees

class BaseTool(abc.ABC):
    name: str                                    # unique, snake_case
    description: str                             # one or two sentences, for the LLM
    keywords: tuple[str, ...]                    # for search_tools matching
    parameters: dict                             # JSON Schema for OAI "function.parameters"
    core: bool = False                           # True = always in context

    @abc.abstractmethod
    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult

@dataclasses.dataclass
class ToolContext:                               # injected per call
    user_id: str
    event: Event
    session: Session
    router: ResponseRouter
    memory: BaseMemory
    tasks: TaskStore
    registry: "ToolRegistry"
    services: dict[str, object]                  # named plugin services, e.g. "home_assistant", "sip"

class ToolRegistry:
    def register(self, tool: BaseTool) -> None
    def core_tools(self) -> list[BaseTool]
    def search(self, query: str, limit: int = 5) -> list[BaseTool]
    def get(self, name: str) -> BaseTool | None
    def to_oai_schema(self, tools: list[BaseTool]) -> list[dict]
```

`search` scoring: `score = |tokenize(query) ∩ (keywords ∪ tokenize(name))|`;
return non-core tools with score ≥ 1, sorted by score desc then name, top
`limit`. `tokenize` = lowercase, split on non-alphanumerics, drop words < 3 chars.

`services` is how tools reach plugin-owned clients (HA WebSocket client, SIP
client) without importing plugins: `main.py` places constructed service objects
into the dict under the names given in §7.

### 4.9 `core/llm/client.py`

```python
class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout_s: float = 60.0, max_retries: int = 2) -> None
    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   temperature: float = 0.7) -> dict   # raw OAI response message
```

`httpx.AsyncClient`, POST `{base_url}/chat/completions`. Retries on network
errors and 5xx with 2 s backoff. Raises `LLMError` after exhausting retries.
No streaming.

### 4.10 `core/agent.py` — the event handling cycle (exact algorithm)

```python
class Agent:
    def __init__(self, llm: LLMClient, registry: ToolRegistry,
                 sessions: SessionManager, tasks: TaskStore,
                 memory: BaseMemory, router: ResponseRouter,
                 services: dict[str, object], persona: str,
                 max_tool_iterations: int = 10) -> None
    async def handle(self, event: Event) -> None
```

`handle(event)` steps — implement in this order, nothing more:

1. `session = sessions.touch(...)` per the channel mapping in §4.6.
2. `open_tasks = await tasks.list_open(event.user_id)`.
3. `system = build_system_prompt(persona, now, session, open_tasks)` (§4.11).
4. Render the event as a user message:
   - `type == "speech"`, `"sip_message"`, `"cli_input"`, `"call_speech"`, or
     `"web_message"`: the transcript/text itself.
   - anything else: `"[event] source=<source> type=<type> payload=<compact json>"`.
5. `messages = [system] + session.history + [event_message]`.
6. `active = registry.core_tools()`; `iterations = 0`.
7. Loop: call `llm.chat(messages, to_oai_schema(active))`.
   - If the reply has tool calls: execute each **sequentially** via
     `registry.get(name).run(ctx, **args)`; unknown tool or bad args →
     `ToolResult(ok=False, content="error: ...")` (never raise). Append the
     assistant message and tool results to `messages`. If a call was
     `search_tools`, extend `active` with the returned tools (deduped).
     `iterations += 1`; if `iterations >= max_tool_iterations`, append a user
     message `"Tool budget exhausted. Respond now without tools."` and loop
     once more with `tools=None`, then treat as final.
   - If the reply has content and no tool calls: final.
8. Final assistant text handling:
   - If the event was user-initiated (step-4 first case) and **no `speak` tool
     call happened** during the loop: `await router.speak(user_id, text)` —
     the user must always get a reply to direct input.
   - If the event was ambient and no speak/notify was called: do nothing
     (empty/short final text means the agent chose silence). Log it.
9. `sessions.append_history(user, "user", event_message_text)` and
   `append_history(user, "assistant", final_text or "<acted via tools>")`.
10. Entire body wrapped in try/except: log exception with event id; if
    user-initiated, attempt `router.speak(user, "Something went wrong handling that.")`.

The active tool set is rebuilt at step 6 **every cycle** — discovery never
persists across events.

### 4.11 `core/prompt.py`

One function, one f-string template:

```python
def build_system_prompt(persona: str, now: datetime, session: Session,
                        open_tasks: list[Task]) -> dict   # {"role":"system", ...}
```

Template content, in order: persona text; current local date/time and timezone;
`Active channel: {channel}. User's current room: {room or "unknown"}.`;
open tasks as `- [{type}] {title} (id={id})` lines or `No open tasks.`; then this
fixed instruction block (verbatim):

> You act through tools. Use `speak` to talk to the user; plain text replies are
> not delivered. Use `search_tools` to find capabilities you don't currently
> have — memory, home control, calendar, weather, communications. Check memory
> before claiming you don't know something about the user or the home. Create a
> task whenever you are waiting on something or someone. Keep spoken replies
> brief and natural. If an ambient event needs no action, reply with the single
> word: IGNORE.

### 4.12 `core/memory/`

```python
class BaseMemory(abc.ABC):
    async def grep(self, pattern: str, scope: str = "all") -> str      # scope: all|short|long
    async def read(self, rel_path: str) -> str
    async def write(self, rel_path: str, content: str, mode: str = "append") -> str  # mode: append|overwrite
    async def list(self) -> str
    async def delete(self, rel_path: str) -> str
    async def prune_short_term(self, retention_days: int) -> int
```

`FilesystemMemory(root: Path)`:

- All `rel_path`s resolved against `root`; reject (return an error string, don't
  raise) anything escaping root, absolute paths, or non-`.md` extensions.
- `grep`: `asyncio.create_subprocess_exec("grep", "-ri", "-n", "-C", "1", pattern, <dir>)`;
  cap output at 4000 chars with a `"...truncated"` marker; exit code 1 → `"No matches."`.
- `write` in append mode adds a leading `\n` and creates parent dirs; returns
  `"Written to {rel_path}."`.
- `list`: recursive listing with file sizes, one per line.
- `prune_short_term`: delete `short-term/*.md` whose filename date is older
  than the retention window; called daily by the scheduler source (§7.2).
- File I/O via `asyncio.to_thread`.

### 4.13 `core/tasks/store.py`

SQLite via `aiosqlite`, file `instance/tasks/tasks.db`, WAL mode.

```sql
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN
    ('awaiting_response','monitoring','reminder_pending','multi_step','deferred')),
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  due_at TEXT,                 -- ISO8601 UTC, for reminder_pending
  trigger TEXT,                -- free-text condition description, for deferred/monitoring
  data TEXT NOT NULL DEFAULT '{}',   -- JSON blob
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT,
  resolution TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_open ON tasks(user_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, due_at);
```

```python
@dataclasses.dataclass
class Task: ...                                   # mirrors columns

class TaskStore:
    def __init__(self, db_path: Path) -> None
    async def init(self) -> None                  # create tables
    async def create(self, user_id, type, title, detail="", due_at=None,
                     trigger=None, data=None) -> Task
    async def update(self, task_id: str, **fields) -> Task | None
    async def close(self, task_id: str, resolution: str) -> Task | None
    async def list_open(self, user_id: str) -> list[Task]
    async def list_due(self, now: datetime) -> list[Task]    # open reminder_pending, due_at <= now
    async def history(self, user_id: str, limit: int = 20) -> list[Task]
```

---

## 5. Core Tools (always in context)

All defined in `plugins/tools/core_tools.py`, all `core = True`. Six tools, no more.

| name | parameters (JSON Schema properties) | behaviour |
|---|---|---|
| `speak` | `message: string` (required) | `await ctx.router.speak(ctx.user_id, message)`. Returns `ok=True, "Delivered."` or `ok=False` on router failure. |
| `send_notification` | `message: string` (required) | `await ctx.router.notify(...)`. |
| `search_tools` | `query: string` (required) | `ctx.registry.search(query)`. Content = one line per match: `name — description`. Agent adds matches to the active set (§4.10 step 7). Empty → `"No tools matched. Try different words."` |
| `get_active_tasks` | none | `ctx.tasks.list_open(ctx.user_id)` rendered one per line with id, type, title, due_at. |
| `create_task` | `type: enum[5 types]`, `title: string`, `detail: string?`, `due_at: string? (ISO8601)`, `trigger: string?` | `ctx.tasks.create(...)`. Content = `"Task {id} created."` |
| `close_task` | `task_id: string`, `resolution: string` | `ctx.tasks.close(...)`. `"Task closed."` or `ok=False, "No such open task."` |

---

## 6. Discoverable Tools (loaded via `search_tools`)

All `core = False`. Keywords shown are the minimum set; implementers may add
synonyms but never remove listed ones.

### 6.1 `memory_tools.py`

| name | params | keywords | behaviour |
|---|---|---|---|
| `memory_grep` | `pattern: string`, `scope: enum[all,short,long] = all` | memory, remember, recall, search, grep, know, history | `ctx.memory.grep` |
| `memory_read` | `path: string` | memory, read, file, notes | `ctx.memory.read`. Reading `INDEX.md` first is encouraged in the tool description. |
| `memory_write` | `path: string`, `content: string`, `mode: enum[append,overwrite] = append` | memory, write, save, remember, note, learn | `ctx.memory.write`. Description instructs: tag entries with `<!-- tags: ... -->`, keep INDEX.md updated after structural changes, daily log goes to `short-term/YYYY-MM-DD.md`. |
| `memory_list` | none | memory, list, files, index | `ctx.memory.list` |
| `memory_delete` | `path: string` | memory, delete, remove, forget, prune | `ctx.memory.delete` |

### 6.2 `task_tools.py`

| name | params | keywords |
|---|---|---|
| `update_task` | `task_id`, plus optional `title`, `detail`, `due_at`, `trigger`, `data (object)` | task, update, change, postpone, edit |
| `get_task_history` | `limit: integer = 20` | task, history, closed, past, previous |

### 6.3 `home_tools.py` — service: `ctx.services["home_assistant"]`

| name | params | keywords | behaviour |
|---|---|---|---|
| `get_home_state` | `entity_id: string?`, `domain: string?` | home, state, status, temperature, light, door, sensor, house | One entity's state, or all states in a domain (capped at 50 lines). No args → the filtered snapshot summary. |
| `control_device` | `entity_id: string`, `action: string`, `value: string?` | home, control, turn, switch, light, set, heating, lock, open, close | Maps to `call_service` (e.g. action `turn_on`, `set_temperature`+value). Returns HA's result. |
| `list_devices` | `domain: string?` | home, devices, entities, list, available | Entity ids + friendly names, capped at 80 lines. |
| `camera_snapshot` | `camera_entity: string` | camera, snapshot, look, see, check, picture, view | Fetches a snapshot via HA REST, saves to a temp file, returns `ok=True` with a short text description path note. v1: the LLM gets `"Snapshot saved: {path} ({bytes} bytes)"` — vision analysis is out of scope. |

If the service is missing from `ctx.services`, every home tool returns
`ok=False, "Home Assistant is not configured."`

### 6.4 `time_tools.py`

| name | params | keywords | behaviour |
|---|---|---|---|
| `get_weather` | `when: enum[now,today,tomorrow] = now` | weather, rain, temperature, forecast, outside, umbrella | Open-Meteo public API (no key) using configured lat/lon. |
| `get_calendar` | `days_ahead: integer = 1` | calendar, events, schedule, appointments, agenda, meeting | CalDAV via `caldav` lib, read events in window. Not configured → `ok=False` message. |
| `add_calendar_event` | `title`, `start: ISO8601`, `end: ISO8601?`, `description?` | calendar, add, create, event, appointment, schedule, book | Creates event via CalDAV. |

### 6.5 `comms_tools.py` — service: `ctx.services["sip"]`

| name | params | keywords | behaviour |
|---|---|---|---|
| `place_call` | `message: string` | call, phone, ring, dial, urgent, reach | Instructs the SIP plugin to dial the user's configured SIP URI, speak `message` via TTS, then listen (§7.5). Returns immediately with `"Call initiated."`; the user's spoken reply arrives later as a new event. |
| `send_sip_message` | `message: string` | sip, text, message, send, sms | SIP MESSAGE to the user's URI. |

---

## 7. Source, Channel & Critical Plugins

Every source validates its own `config` dict in `__init__` and raises
`ConfigError` (from `core.config`) on bad config — this fails startup, which is
correct: bad config should never half-run.

### 7.1 `sources/cli.py` + `channels/console.py`  (built first — the dev surface)

- `CLISource(name="cli")`: reads lines from stdin (`asyncio` reader). Each
  non-empty line → event `type="cli_input"`, `priority=NORMAL`,
  `payload={"text": line}`. Line starting with `!high ` → HIGH. `!quit` stops
  the daemon.
- `ConsoleChannel(type=CONSOLE)`: prints `ARES> {message}`. Always returns True.

### 7.2 `sources/scheduler.py`

`SchedulerSource(name="scheduler")`. Two jobs, both simple `asyncio.sleep` loops
(no cron library):

1. Every 60 s: `tasks.list_due(now)` → for each, emit
   `type="task_due"`, `priority=HIGH`, `payload={"task_id", "title", "detail"}`
   for the task's user. Mark the task's `data.fired = true` via `update` so it
   emits only once (the LLM closes or reschedules it).
2. Daily at the configured local time (default 03:30): call
   `memory.prune_short_term(retention_days)` and emit a LOW `type="housekeeping"`
   event with the prune count.

### 7.3 `sources/home_assistant.py`

`HomeAssistantSource(name="home_assistant")`, plus a `HAService` object placed
in `services["home_assistant"]` exposing:

```python
async def get_state(entity_id) -> dict
async def get_states(domain: str | None) -> list[dict]
async def call_service(domain, service, entity_id, data: dict) -> dict
async def snapshot_summary() -> str          # cached, rebuilt at most every 30 s
async def camera_snapshot(entity_id) -> Path
```

Source behaviour:

- Connect to `ws_url` with the token; authenticate; `subscribe_events`
  (`state_changed`); reconnect with exponential backoff (2→60 s) forever.
- **Filter layer** (all config-driven, defaults shown in §8):
  - allow-list of domains (`binary_sensor`, `person`, `alarm_control_panel`, plus
    configured extras) and an allow-list of specific `entity_id`s;
  - drop events where `old_state.state == new_state.state`;
  - debounce: per entity, suppress events within `debounce_seconds` (default 5)
    of the last emitted one;
  - map to priority via config rules (`entity glob or domain → priority`);
    default NORMAL. Anything mapped CRITICAL bypasses the LLM (§4.3).
- Emitted event: `type="state_change"`,
  `payload={"entity_id", "old", "new", "friendly_name", "snapshot": <summary str>}`,
  `room` from configured entity→room map when available.
- `person.*` entity changes additionally update `session.current_room`/away
  status via `SessionManager.touch` (room `None` when away).
- Custom HA events on type `ares_event` pass through verbatim with the payload's
  declared priority (e.g. face recognition from Frigate/CompreFace automations:
  `{"event":"face_recognised","who":"John","location":"front_door","priority":"NORMAL"}`).

### 7.4 `sources/voice/` + `channels/voice_tts.py`

One `VoiceSource` instance **per configured room**; name `voice`.

Pipeline per room (audio in via `sounddevice` input stream on the configured
device):

1. `vad.py`: Silero VAD (via `torch.hub` cached locally or the `silero-vad`
   pip package) on 30 ms frames; speech segment = speech start → 700 ms of
   silence; hard cap 30 s.
2. `stt.py`: `faster-whisper` (`WhisperModel`, model size from config, CPU int8
   default) transcribes the segment. Empty/whitespace transcript → drop.
3. `intent.py`: strategy from config:
   - `wake_word`: openWakeWord runs on the raw audio in parallel with VAD;
     a segment passes only if the wake word fired within it or ≤ 2 s before it.
   - `llm`: one `LLMClient.chat` call with a fixed classification prompt
     ("Reply YES or NO: is this utterance addressed to a home assistant named
     ARES? ...transcript..."); pass on YES.
   - `hybrid`: wake word pass OR llm pass.
4. Pass → emit `type="speech"`, `priority=NORMAL`,
   `payload={"text": transcript}`, `room=<room>`, `user_id="primary"`.

`VoiceTTSChannel(type=VOICE)`: `deliver` resolves the room from
`session.current_room` **at delivery time** (fallback: last event room, then the
configured default room), synthesises with Piper (subprocess `piper --model ...`
producing WAV), plays via `sounddevice` on that room's output device. While TTS
is playing in a room, that room's VAD is muted (shared per-room `asyncio.Event`
exposed by the voice plugin) to stop ARES hearing itself.

### 7.5 `plugins/sip/` + sip channels

`sip/client.py`: one `SIPService` (registered in `services["sip"]`) wrapping
pjsua2: account registration to the Asterisk server, plus:

```python
async def send_message(uri: str, text: str) -> bool
async def call_and_speak(uri: str, text: str, listen: bool) -> None
def on_incoming_call(cb) / def on_incoming_message(cb)
```

`sip/source.py` (`SIPSource`, name `sip`):

- Incoming SIP MESSAGE → event `type="sip_message"`, NORMAL,
  `payload={"text", "from_uri"}`.
- Incoming call from the configured user URI: answer, play greeting via Piper,
  then loop: record until 700 ms silence (or timeout as configured) → Whisper → emit
  `type="call_speech"`, NORMAL. TTS replies stream back into the call via the
  `SIPCallChannel` while the call is up. Hang-up ends the loop.
- Calls from unknown URIs: reject, log.

`SIPMessageChannel(type=SIP_MESSAGE)`: `send_message(user_uri, text)`.
`SIPCallChannel(type=SIP_CALL)`: delivers into the active call; returns False
if no call is active (router then falls back to PUSH).

pjsua2 is callback/thread based: all callbacks marshal onto the main loop with
`run_coroutine_threadsafe`. All audio bridging uses PJSIP's WAV player/recorder
ports against temp files — do not attempt live sample streaming in v1.

### 7.6 `channels/push_ntfy.py`

`NtfyChannel(type=PUSH)`: POST the message to `{server}/{topic}` with optional
auth token header. Returns False on non-2xx.

### 7.7 `critical/safety.py`

Two handlers, registered when the plugin is enabled:

- `FireHandler`: matches `state_change` events whose entity matches the
  configured smoke/fire entity globs and new state is `on`/`triggered`.
  Action: TTS broadcast of a fixed phrase to **all** voice rooms, push
  notification, SIP call attempt if user away, create `monitoring` task.
- `IntruderHandler`: same pattern for alarm entities.

All actions inside are direct channel/service calls — no LLM, no tools.

---

## 8. Configuration (`instance/config.yaml` — full reference example)

```yaml
persona: |
  You are ARES, the household's resident AI. Dry, concise, competent.
  You know the house, the routines, and the user. You never waffle.

timezone: Europe/London

llm:
  base_url: http://ollama.local:11434/v1
  api_key: !secret LLM_API_KEY          # "ollama" is fine for Ollama
  model: qwen2.5:32b-instruct
  temperature: 0.7
  max_tool_iterations: 10

session:
  history_limit: 30
  timeout_minutes: 45

memory:
  root: instance/memory
  retention_days: 14

tasks:
  db_path: instance/tasks/tasks.db

users:
  primary:
    sip_uri: sip:me@asterisk.local
    ntfy_topic: ares-primary

plugins:
  cli:
    enabled: true
  console_channel:
    enabled: true
  scheduler:
    enabled: true
    housekeeping_time: "03:30"
  push_ntfy:
    enabled: false
    server: https://ntfy.local
    token: !secret NTFY_TOKEN
  home_assistant:
    enabled: false
    ws_url: ws://ha.local:8123/api/websocket
    rest_url: http://ha.local:8123
    token: !secret HA_TOKEN
    allowed_domains: [binary_sensor, person, alarm_control_panel]
    allowed_entities: []
    debounce_seconds: 5
    priority_rules:
      - match: "binary_sensor.smoke_*"
        priority: CRITICAL
      - match: "alarm_control_panel.*"
        priority: CRITICAL
      - match: "binary_sensor.front_door"
        priority: HIGH
    entity_rooms:
      binary_sensor.kitchen_motion: kitchen
  voice:
    enabled: false
    whisper_model: small
    intent_strategy: hybrid            # wake_word | llm | hybrid
    wake_word: "hey_ares"
    default_room: living_room
    rooms:
      kitchen:    { input_device: "USB Audio 1", output_device: "USB Audio 1" }
      living_room:{ input_device: "USB Audio 2", output_device: "USB Audio 2" }
    piper_model: en_GB-alan-medium
  sip:
    enabled: false
    server: asterisk.local
    username: ares
    password: !secret SIP_PASSWORD
    greeting: "ARES. Go ahead."
  time_tools:
    enabled: true
    latitude: 51.32
    longitude: -0.56
    caldav_url: ""
    caldav_username: ""
    caldav_password: !secret CALDAV_PASSWORD
  safety_critical:
    enabled: false
    fire_entities: ["binary_sensor.smoke_*"]
    alarm_entities: ["alarm_control_panel.*"]
  shell:
    enabled: true
    sandbox_user: ""                 # "" = run as daemon user (DEV ONLY, logs a warning)
                                     # production: ares-sbx
    workdir: /home/ares-sbx          # dev: any scratch dir
    timeout_default_s: 30
    timeout_max_s: 120
  privileges:
    enabled: true
    db_path: instance/privq.db       # production: /var/lib/ares/privq.db
  dashboard:
    enabled: true
    host: 0.0.0.0
    port: 8788
    password: !secret DASHBOARD_PASSWORD
  selfedit:
    enabled: false
    live_code_path: /opt/ares/app    # read-only view of running code
    scratch_repo: /home/ares-sbx/scratch/ares
    github_repo: youruser/ares       # owner/name
    github_token: !secret GITHUB_TOKEN   # fine-grained: PRs read/write only
```

In production the config file lives at `/etc/ares/config.yaml` and the state
paths point into `/var/lib/ares` (§14). The daemon takes the config path as its
single CLI argument, defaulting to `instance/config.yaml`.

`instance/main.py` (the only file with instance wiring): load config → build
core objects → for each enabled plugin, construct and register its
sources/channels/tools/handlers/services → start dispatcher + source supervisors
→ run until signal → stop sources, close task DB.

`.env.example` lists every `!secret` key used above with placeholder values.

---

## 9. Initial Memory Content

Ship these files verbatim in `instance/memory/`:

**`INDEX.md`**
```markdown
# Memory Index
Maintained by ARES. One line per file: path — what it contains.

- long-term/preferences.md — user preferences and dislikes
- long-term/people.md — people, visitors, relationships
- long-term/routines.md — observed schedules and habits
- long-term/home.md — rooms, devices, configuration facts
- short-term/ — daily interaction logs (auto-pruned)
```

Each `long-term/*.md` starts with a title line and a
`<!-- tags: ... -->` line and is otherwise empty.

---

## 10. Build Order & Acceptance Tests

Do these strictly in order. A milestone is done when its acceptance test passes
and `PROGRESS.md` is updated.

**M0 — Skeleton.** pyproject (deps: core group only), package dirs, `utils/`
(logging, ids, text), `secrets.py`, `config.py` with the full Pydantic model,
`.env.example`, empty memory tree, `PROGRESS.md` initialised.
*Accept:* `python -c "from ares.core.config import load_config; ..."` loads the
example config with a stub `.env` and prints a typed object; a `!secret` for a
missing key raises `SecretNotFound`.

**M1 — Bus + CLI loop (no LLM).** `event.py`, `source.py`, `channel.py`,
`router.py`, `session.py`, `dispatcher.py`, `critical.py`, CLI source, console
channel, a temporary echo agent stub. *Accept:* run `instance/main.py`, type
`hello`, see `ARES> echo: hello`; `!quit` exits cleanly.

**M2 — Real agent.** `llm/client.py`, `tool.py`, `prompt.py`, `agent.py`,
`core_tools.py` (all six; `search_tools` works but the registry only has core
tools yet). *Accept:* conversation over CLI against a live OAI endpoint; model
replies arrive via the `speak` tool; direct input always gets a reply; tool-loop
budget enforced.

**M3 — Memory.** `memory/base.py`, `memory/filesystem.py`, `memory_tools.py`,
initial memory files. *Accept:* "remember that I like the house at 20 degrees"
→ file written with tags; new session, "what temperature do I like?" → correct
answer via `search_tools` → `memory_grep`. Path-escape attempts rejected.

**M4 — Tasks.** `tasks/store.py`, `task_tools.py`, scheduler source (both jobs).
*Accept:* "remind me in 2 minutes to check the oven" → `reminder_pending` task
→ ~2 min later a `task_due` HIGH event → ARES speaks the reminder and closes
the task. Short-term memory pruning runs at the configured time (test with a
near-future time).

**M5 — Sessions & routing.** Push channel (ntfy), channel-switch behaviour.
*Accept:* ask ARES something on CLI mid-conversation, then simulate a channel
switch (send an event with a different source type in a test); reply goes to the
new channel; PUSH fallback fires when a channel's `deliver` returns False.

**M6 — Home Assistant.** Source, filter layer, `HAService`, home tools,
`critical/safety.py`. *Accept:* against a real or mocked HA: state changes pass
the filter with debounce; "turn on the kitchen light" works end-to-end; a
smoke-entity `on` event triggers the deterministic handler with no LLM call
(assert via logs).

**M7 — Voice.** VAD, STT, intent filter, `VoiceSource`, `VoiceTTSChannel`.
*Accept:* in one configured room: speak with wake word → transcribed → agent →
TTS reply in that room; ambient speech without wake word is dropped; VAD muted
during playback.

**M8 — SIP.** `SIPService`, source, both channels, `comms_tools.py`.
*Accept:* SIP MESSAGE to ARES gets a MESSAGE reply; calling the extension gets
greeting + two-way conversation; `place_call` from a CLI instruction rings the
softphone and speaks.

**M9 — Time tools + hardening.** `time_tools.py`; source supervisor
restart-with-backoff verified; graceful shutdown audited; README written.
*Accept:* weather answer end-to-end; kill a source's socket and watch it restart;
Ctrl-C exits within 5 s with all tasks cancelled cleanly.

**M10 — Sandboxed shell + privilege queue + broker.** `shell_tools.py`,
`privileges/store.py`, `privileges/tools.py`, `privileges/source.py`,
`broker/aresbrokerd.py`, `broker.example.json`. *Accept (dev mode):* `run_shell`
executes and returns output with the sandbox-not-configured warning;
`request_privilege` files a pending row; approving it in the DB (simulating the
dashboard) makes the broker execute an allowlisted `package_install` and mark it
done; a non-allowlisted approved command is rejected by the broker; a
`privilege_update` event reaches the agent. Broker imports nothing from `ares`
(assert). Unit tests: PrivStore state machine, broker allowlist regex accept/reject,
argv construction never uses shell=True.

**M11 — Web dashboard.** `dashboard/server.py`, `dashboard/api.py`,
`dashboard/channel.py`, `dashboard/static/index.html`. *Accept:* dashboard serves
with token auth; chat round-trips (post → agent → long-poll reply via WebChannel);
memory list/read shows files RO; tasks list renders; a pending priv request shows
with working Approve/Deny buttons that flip DB status; health endpoint returns
queue depths. Path-escape on `/api/memory/file` rejected.

**M12 — Self-edit → PR.** `selfedit_tools.py`. *Accept (against a real test
repo):* `open_pr` creates a branch, commits given files into the scratch clone
only, pushes, and opens a PR; `/opt/ares` (or its dev stand-in) is never
written; `get_pr_status` reports state; path-escape in `files` rejected; PR
appears in `/api/prs`. Verify with branch protection on that ARES cannot merge.

**M13 — Update listener + deploy artifacts.** `updater/aresupdater.py`,
`deploy/provision.sh`, the three `.service` units, `ARES_ENV=prod` tripwires in
`config.py`/shell/secrets. *Accept:* updater detects a new `main` SHA via poll,
runs the smoke import, swaps the release symlink, and issues the restart command
(mock the restart in test); webhook HMAC verification accepts a correctly-signed
payload and rejects a bad one; a failing smoke import aborts the swap; updater
imports nothing from `ares` (assert). `provision.sh` passes `shellcheck` and
`bash -n`. In `prod` mode, a readable `.env` or missing sandbox user causes fatal
startup (test the tripwire).

Tests: pytest + pytest-asyncio. Unit tests required for: tool registry search
scoring, memory path safety, task store CRUD + `list_due`, config `!secret`
resolution, session timeout/history trim, dispatcher LOW-drop and HIGH-front
policies, HA filter/debounce, PrivStore state machine, broker allowlist
accept/reject + argv construction, dashboard token auth + memory path safety,
self-edit path-escape rejection, updater HMAC verify + smoke-import-abort +
prod tripwires. Plugins needing hardware (voice, SIP) get import-level and
config-validation tests only. The broker and updater get their own tests that
assert they import nothing from `ares` (parse the module, check imports).

---

## 11. PROGRESS.md Protocol

The implementing agent maintains `PROGRESS.md` at repo root. Format:

```markdown
# ARES Build Progress
Spec: ARES-SPEC.md v1.0. Read §0 rules before every session.

## Current
Milestone: M3. Next action: implement memory/filesystem.py grep().

## Ticklist
### M0 — Skeleton
- [x] pyproject.toml
- [x] ares/core/utils/logging.py
...
### M3 — Memory
- [x] ares/core/memory/base.py
- [ ] ares/core/memory/filesystem.py
...

## Decisions
- 2026-07-11: used shutil.which("grep") check, fallback to Python re scan. (§4.12 silent on grep absence)

## Blockers
- (none)
```

Rules: (1) update the ticklist and `## Current` after **every** completed file;
(2) never delete or rewrite completed entries; (3) every deviation or
spec-silence decision gets a dated line in `## Decisions`; (4) anything that
would require a new dependency or a spec change goes in `## Blockers` and work
moves to the next unblocked item; (5) at the start of any session, read
`## Current` first and resume exactly there.

The full ticklist (every file from §3, grouped by milestone) must be generated
into `PROGRESS.md` during M0 so later sessions never have to re-derive it.

---

## 12. Dependencies (complete list — nothing else)

Core (always installed): `pydantic>=2`, `PyYAML`, `python-dotenv`, `httpx`,
`aiosqlite`.

Optional extras in `pyproject.toml`:
- `voice`: `faster-whisper`, `silero-vad`, `openwakeword`, `sounddevice`, `numpy`
- `sip`: `pjsua2` (system PJSIP build; document in README)
- `calendar`: `caldav`
- `dashboard`: `fastapi`, `uvicorn`
- `dev`: `pytest`, `pytest-asyncio`

Self-edit and updater use `git` (system binary) and the GitHub REST API over
the already-present `httpx`; no GitHub SDK. The **broker and updater use only
the Python standard library** (they run at higher privilege — no third-party
attack surface). Piper is an external binary; grep and git are system binaries.

---

## 14. Runtime & Security Model (Firecracker microVM)

ARES runs inside one Firecracker microVM on the host. Inside that VM the
privilege split below is the **security boundary the whole design depends on**.
The implementing agent does not build Firecracker orchestration (operator's job,
`DEPLOYMENT.md`); it builds code that runs *correctly under* these constraints
and never assumes more privilege than it has.

### 14.1 Users inside the VM

| User | Runs | Can read | Can write | sudo |
|---|---|---|---|---|
| `ares` | the ARES daemon (`ares.service`) | `/opt/ares/app` (RO), `/var/lib/ares` | `/var/lib/ares` only | **none** |
| `ares-sbx` | sandboxed shells & the scratch clone | its own `$HOME` | its own `$HOME` only | **none** |
| `ares-deploy` | update listener (`ares-updater.service`) | `/opt/ares` | `/opt/ares` | restart ares unit only |
| `root` | the broker (`ares-broker.service`) | everything | everything | n/a |

### 14.2 Filesystem contract

- **`/opt/ares/app`** — the live running code. Owned by `ares-deploy:ares`,
  mode `0750`, **read-only to the `ares` daemon**. ARES can read its own source
  (self-inspection) but the OS prevents it editing what it runs.
- **`/etc/ares/`** — config + secrets. `config.yaml` is `0640 root:ares`
  (readable). **`.env` (secrets) is `0600 root:root` — the `ares` user cannot
  read it.** Secrets are injected into the daemon's environment by systemd
  `EnvironmentFile=` at unit start (systemd reads it as root before dropping to
  the `ares` user), so the process has the values but the *file* is unreadable
  to that user. `EnvSecretStore` therefore reads from `os.environ`, and only
  falls back to a dotenv file in dev (§14.4).
- **`/var/lib/ares/`** — all mutable state: `tasks.db`, `privq.db`, `memory/`,
  logs, snapshot temp. Owned `ares:ares`, `0700`.
- **`/home/ares-sbx/`** — sandbox home + scratch clone. Owned `ares-sbx`.
  The `ares` daemon can traverse in to spawn processes as `ares-sbx` via the
  broker/runner (§15) but does not own it.

### 14.3 What each ARES capability may touch

- Memory / tasks / privilege queue → `/var/lib/ares` (daemon writable). OK.
- Shell tool → executes **as `ares-sbx`**, never as `ares`. §15.
- Self-edit → operates **only** in `/home/ares-sbx/scratch/ares`, produces a
  PR. Never writes `/opt/ares`. §18.
- System changes (package installs, unit changes) → only via the privilege
  queue → broker, human-approved. ARES itself has no sudo. §16.

### 14.4 Dev vs production

Everything ships able to run in a plain dev checkout with no VM:
`sandbox_user=""` runs shells in-process user (with a loud warning),
secrets fall back to `instance/.env`, state paths stay under `instance/`. The
`ARES_ENV` env var (`dev` default, `prod`) selects behaviour; in `prod` the
absence of the user separation (e.g. `.env` readable, no `ares-sbx`) is a
**fatal startup error**, not a warning. This tripwire prevents shipping a
misconfigured VM that silently runs everything as one user.

---

## 15. Sandboxed Shell (`plugins/tools/shell_tools.py`)

One discoverable tool. This is the only way ARES runs arbitrary commands, and it
**never runs them as its own user**.

```
run_shell(command: string, timeout_s: integer? = 30) -> ToolResult
  keywords: shell, run, command, execute, bash, script, terminal, cli
```

Behaviour:

- Rejects (returns `ok=False`, no execution) if `command` is empty or
  `timeout_s > timeout_max_s`.
- Execution model:
  - **prod:** `sudo -n -u {sandbox_user} /bin/bash -lc {command}` via
    `asyncio.create_subprocess_exec`, cwd = `workdir`, with a restricted env
    (PATH, HOME, LANG only — never the ARES secret env). The single sudoers
    entry permitting `ares → ares-sbx` (NOPASSWD, that exact runner) is created
    by `provision.sh`; this is drop-privilege only, never escalation.
  - **dev (`sandbox_user=""`):** run directly, same restricted env, and prepend
    a one-line warning to the result that the sandbox user is not configured.
- Kill the process group on timeout; return combined stdout+stderr capped at
  4000 chars + exit code. Non-zero exit is `ok=True` with the output (the LLM
  decides what a failure means) — infra failures (spawn error, timeout) are
  `ok=False`.
- The tool description tells the LLM: this runs as an unprivileged sandbox user
  with no access to secrets or the live code; to install packages or change the
  system, use `request_privilege`; to change ARES's own code, use `open_pr`.

**The daemon must never call `run_shell` logic against its own uid.** If, in
prod, the effective runner would be the `ares` user, refuse and log ERROR.

---

## 16. Privilege Escalation Queue + Root Broker

ARES has no sudo. When it wants a privileged action it files a **request**; a
separate **root broker** process executes only human-approved, allowlisted
requests. The daemon and the broker share a SQLite DB and never call each other.

### 16.1 `plugins/privileges/store.py` — `PrivStore` (aiosqlite)

DB `/var/lib/ares/privq.db` (dev: `instance/privq.db`), owned `ares:ares` but
the broker (root) can read/write it too.

```sql
CREATE TABLE IF NOT EXISTS priv_requests (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('package_install','service_action','command')),
  command TEXT NOT NULL,          -- exact argv-as-string the broker will run
  reason TEXT NOT NULL,           -- ARES's justification, shown to operator
  status TEXT NOT NULL DEFAULT 'pending'
     CHECK (status IN ('pending','approved','denied','executing','done','failed')),
  exit_code INTEGER,
  output TEXT,                    -- capped result, readable by ARES
  created_at TEXT NOT NULL,
  decided_at TEXT,
  executed_at TEXT
);
```

`PrivStore` methods: `init`, `create(user_id, kind, command, reason) -> req`,
`list(status=None) -> list`, `get(id)`, `approve(id)`, `deny(id)` (dashboard
only), `mark_executing/mark_done/mark_failed` (broker only). Approve/deny set
`decided_at`. ARES code **never** sets status to `approved` — only the dashboard
(operator action) does. Enforce this by keeping `approve`/`deny` off the tool
surface (§16.2) — they exist on the store but are called only from dashboard
routes.

### 16.2 `plugins/privileges/tools.py` (discoverable tools)

```
request_privilege(kind: enum[package_install,service_action,command],
                  command: string, reason: string) -> ToolResult
  keywords: sudo, install, package, privilege, permission, system, apt, root, admin
  -> creates a pending request; returns "Request {id} filed; awaiting approval."

get_privilege_requests(status: string? = null) -> ToolResult
  keywords: privilege, requests, pending, approved, sudo, status
  -> lists requests with id/kind/status/command for this user
```

A `PrivilegeSource` (name `privileges`) polls `PrivStore` every 15 s for
requests that transitioned to `done`/`failed`/`denied` since last seen and emits
a NORMAL `type="privilege_update"` event so ARES learns the outcome and can act
(e.g. retry, tell the user, close a related task).

### 16.3 `broker/aresbrokerd.py` — the root broker

**Stdlib only. Never imports `ares`. This is the trusted root component; keep it
tiny and auditable (< 200 lines).**

Loads `/etc/ares/broker.json`:
```json
{
  "db_path": "/var/lib/ares/privq.db",
  "poll_seconds": 5,
  "allow": {
    "package_install": ["^[a-z0-9][a-z0-9+.\\-]*$"],
    "service_action": ["^(restart|status) (ares|ares-updater)$"],
    "command": []
  }
}
```

Loop: every `poll_seconds`, select `status='approved'`. For each:

1. **Re-validate** `command` against the allow-regex for its `kind`. No match →
   `mark_failed` with `output="rejected: not allowlisted"`. This is defence in
   depth: even an operator mis-approval can't run something off-list.
2. Build a fixed argv from `kind` (never `shell=True`, never string-splitting
   attacker-controlled text):
   - `package_install` → `["apt-get","install","-y", <pkg>]` where `<pkg>` is
     the validated token.
   - `service_action` → `["systemctl", <action>, <unit>]` from the two captured
     groups.
   - `command` → only if a future allowlist regex is added; default empty ⇒
     always rejected in v1.
3. `mark_executing`, run with a 300 s timeout, capture output (cap 8000 chars),
   `mark_done`/`mark_failed` with exit code.

The broker writes back only to `priv_requests`. It executes nothing that is not
both `approved` (human) and allowlist-matched (regex). Everything is logged to
`/var/log/ares-broker.log`.

---

## 17. Web Dashboard (`plugins/dashboard/`)

A local FastAPI app (uvicorn) the operator uses to talk to ARES and supervise
it. Bound inside the VM; reached over Tailscale/LAN. Single shared password
(`!secret DASHBOARD_PASSWORD`), sent as a bearer token; no accounts. Polling,
no websockets.

### 17.1 `DashboardSource` + `WebChannel`

- `DashboardSource` (name `dashboard`) validates config and runs uvicorn in the
  same event loop (`uvicorn.Server.serve()` as a task). It owns a `WebChannel`
  and per-user outbox `asyncio.Queue`s.
- `WebChannel(type=WEB)`: `deliver` pushes the message onto the user's outbox
  queue. The browser long-polls `/api/chat/poll` to drain it. This is how the
  agent's `speak` reaches the dashboard when the active channel is WEB.
- Chat in: `POST /api/chat` → emits `type="web_message"`, NORMAL,
  `payload={"text"}`; the session channel flips to WEB (§4.6), so the reply
  routes back to the browser.

### 17.2 API routes (all require the bearer token except `/` and static)

```
GET  /                       -> static/index.html
POST /api/chat               {text}          -> 202, emits web_message
GET  /api/chat/poll          ?since=         -> long-poll (≤25 s) new outbox msgs
GET  /api/memory/list                        -> memory.list()
GET  /api/memory/file        ?path=          -> memory.read() (path-safe; RO)
GET  /api/tasks              ?status=open     -> task list
GET  /api/privileges         ?status=pending  -> priv request list
POST /api/privileges/{id}/approve            -> PrivStore.approve  (operator gate)
POST /api/privileges/{id}/deny               -> PrivStore.deny
GET  /api/prs                                 -> open self-edit PRs (§18 cache)
GET  /api/health                             -> {ok, uptime, queue depths}
```

Memory and PR views are **read-only** from the dashboard. Approve/deny are the
only state-changing supervisory actions, and they are exactly the human gates
for §16 and §18. All routes are thin wrappers over core objects passed in at
construction; no business logic in the API layer.

### 17.3 `static/index.html`

One self-contained file: vanilla JS, no build step, no external CDN. Tabs: Chat
(with long-poll), Memory (list + file view), Tasks, Approvals (pending priv
requests with Approve/Deny buttons + reason/command shown), PRs (open self-edit
PRs, links to GitHub). Token entered once, kept in a JS variable (not
localStorage, per artifact storage rules don't apply here but keep it simple).

---

## 18. Self-Edit → Pull Request Workflow (`plugins/tools/selfedit_tools.py`)

ARES can propose changes to its own code. It can never merge them. The gate is
the operator reviewing and merging the PR on GitHub.

Two discoverable tools:

```
open_pr(branch: string, title: string, body: string,
        files: array<{path: string, content: string}>) -> ToolResult
  keywords: code, edit, self, pr, pull, request, patch, improve, fix, propose
```

Behaviour (all inside `scratch_repo`, as `ares-sbx` via `run_shell`-style
exec, never touching `/opt/ares`):

1. Ensure the scratch clone exists and is clean; `git fetch origin`,
   `git checkout -B {branch} origin/main`.
2. Write each file's `content` to its `path` **within the scratch repo only**
   (reject absolute paths or paths escaping the repo → `ok=False`).
3. `git add -A && git commit -m {title}` (author: `ARES <ares@localhost>`).
4. `git push origin {branch}` using `GITHUB_TOKEN`.
5. Create the PR via GitHub REST (`POST /repos/{repo}/pulls`, base `main`).
   Store the PR number/url in a small in-memory cache for `/api/prs`.
6. Return `ok=True` with the PR URL.

```
get_pr_status(number: integer) -> ToolResult
  keywords: pr, pull, request, status, merged, review, code
  -> GitHub REST GET; reports open/closed/merged so ARES knows if the operator
     accepted its change.
```

Constraints: the fine-grained `GITHUB_TOKEN` grants **PR + contents write on the
one repo, nothing else**; it has no merge-to-`main` if branch protection is on
(the operator enables branch protection — documented in `DEPLOYMENT.md`). ARES
merging its own PR is out of scope and structurally prevented by branch
protection, not by ARES's own restraint.

---

## 19. Update Listener (`updater/aresupdater.py`)

Separate process run as `ares-deploy`. **Stdlib only, never imports `ares`.**
Watches for merged changes on `main` and safely swaps the live code.

Config `/etc/ares/updater.json`:
```json
{
  "repo": "youruser/ares",
  "app_dir": "/opt/ares/app",
  "branch": "main",
  "webhook_port": 8790,
  "webhook_secret_env": "ARES_WEBHOOK_SECRET",
  "poll_seconds": 300,
  "restart_cmd": ["sudo","-n","systemctl","restart","ares"]
}
```

Two triggers, one action:

- **Webhook:** minimal stdlib `http.server` on `webhook_port`, path `/gh`.
  Verifies the `X-Hub-Signature-256` HMAC against `ARES_WEBHOOK_SECRET`. On a
  verified `push` to `main`, triggers an update. (Exposed to GitHub via the
  reverse tunnel documented in `DEPLOYMENT.md`.)
- **Poll fallback:** every `poll_seconds`, `git ls-remote origin main`; if the
  remote SHA differs from the deployed SHA, trigger an update. This covers
  missed webhooks.

Update action (serialised with a lock so webhook+poll can't collide):

1. In a temp worktree: `git fetch origin main`, checkout the new SHA.
2. Install/verify deps into the app venv (`/opt/ares/venv`); run a smoke import
   (`python -c "import ares.core.agent"`). On failure, **abort — do not swap**,
   log, leave the running daemon untouched.
3. Atomically point `/opt/ares/app` at the new tree (swap a symlink
   `app -> releases/<sha>`; keep the previous release for rollback).
4. `restart_cmd` (the one sudoers entry `ares-deploy` holds: restart `ares`).
5. Write the deployed SHA to `/opt/ares/RELEASED_SHA` and log.

Because merge-to-`main` is the human gate (§18), this listener only ever runs
operator-approved code. It never pulls arbitrary branches.


---

## 13. Glossary of Non-Obvious Decisions (context for the implementer)

- **Why the router, not the LLM, picks the channel:** the LLM would need
  live routing state on every call; instead `speak` is transport-blind and the
  router reads the session at delivery time. This is also why voice room
  resolution happens inside the voice channel.
- **Why tool discovery resets per cycle:** keeps token cost flat and prevents
  context bloat over long-running operation. Tasks and memory carry the
  durable state instead.
- **Why LOW events are dropped when busy:** LOW means "nice to know"; queuing
  them creates stale backlogs after busy periods.
- **Why HIGH doesn't interrupt in v1:** cancellation of an in-flight tool loop
  safely (mid device control, mid memory write) needs compensation logic that
  isn't worth it yet. Front-of-queue is enough.
- **Why sessions are in-memory only:** they are ephemeral routing/history
  state; durable continuity is the job of tasks and memory, which survive
  restarts by design.
- **Why the fixed IGNORE convention (§4.11):** gives the agent an explicit,
  loggable way to decline ambient events instead of hallucinating a speak call.
- **Why the broker is a separate root process, not sudo from ARES:** giving the
  `ares` user any sudo entry — even a narrow one — makes the daemon's whole
  attack surface a path to root. Instead ARES (unprivileged) can only *write a
  row*; a tiny, auditable, stdlib-only root process executes only rows that are
  both human-approved and regex-allowlisted. Compromising ARES yields a request
  queue, not a root shell.
- **Why self-edits go through a PR the operator merges, never auto-applied:**
  the security model's foundation is that ARES cannot change the code it runs.
  A PR is a proposal; branch protection makes the human merge the only path to
  `main`; the updater then deploys only `main`. There is deliberately no code
  path from ARES's reasoning to running modified code without a human in between.
- **Why the shell always runs as `ares-sbx`, never `ares`:** the daemon holds
  the (env-injected) secrets and the readable config; a command running as that
  same user could exfiltrate them. Dropping to a secret-less, RO-code sandbox
  user means arbitrary command execution can't reach secrets or live code.
- **Why broker/updater are stdlib-only and never import `ares`:** they run at
  higher privilege than the daemon. Keeping them tiny, dependency-free, and
  independent means a supply-chain issue in ARES's dependency tree can't touch
  the privileged components, and they can be audited in isolation.
- **Why `ARES_ENV=prod` has hard tripwires:** the entire security model is
  invisible at runtime if the users/permissions aren't set up — everything
  "works" running as one user. Failing fast on a readable `.env` or missing
  sandbox user turns a silent security collapse into a loud startup error.

*End of specification.*
