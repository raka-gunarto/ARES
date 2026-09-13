# ARES — Automated Request Execution System

ARES is a self-hosted, always-on, event-driven personal AI agent. It receives events from pluggable sources (voice, Home Assistant, SIP, scheduler, CLI, the web dashboard), reasons about them with an LLM via an OpenAI-compatible API, keeps memory as human-editable markdown files, and maintains open tasks in SQLite for continuity. Replies route dynamically to whichever channel the user is currently on—voice, a Home Assistant speaker, text, push notification, or web. It can browse the web (a one-shot page fetch and a persistent browser you can watch and take over), and hand long work to bounded background subagents.

## Requirements

- **Python ≥ 3.11**
- **A running OpenAI-compatible LLM endpoint** (e.g., Ollama local LLM)
- Optional system binaries:
  - **Piper** (TTS binary for voice output; install separately)
  - **PJSIP** system build (for `[sip]` extra; SIP telephony support)
  - **ntfy server** (for push notifications; self-hosted or ntfy.sh)
  - **Home Assistant** (for home control; reachable via WebSocket)
  - **CalDAV server** (for calendar access; optional time tools)
  - **Chromium** (for the `browser` plugin: `fetch_page` and the stateful `browser`)
  - **grep** and **git** (system binaries, standard on Linux/macOS)

## Install

### Virtual Environment

Always use the repo-local venv. If it doesn't exist:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Verify activation:
```bash
which python  # should end with .venv/bin/python
```

### Optional Extras

Install only what you need:

```bash
# Voice pipeline (speech recognition + TTS engine)
pip install -e ".[voice]"
# Requires: faster-whisper, silero-vad, openwakeword, sounddevice, numpy
# Also: Piper binary installed separately

# SIP telephony (calling, SMS over SIP)
pip install -e ".[sip]"
# Requires: pjsua2 Python bindings + system PJSIP build

# Calendar integration (CalDAV)
pip install -e ".[calendar]"
# Requires: caldav

# Web dashboard
pip install -e ".[dashboard]"
# Requires: fastapi, uvicorn
```

**No other dependencies are permitted** (see `ARES-SPEC.md` §12).

## Configuration

1. Copy the example config:
   ```bash
   cp instance/.env.example instance/.env
   ```

2. Edit `instance/.env` and fill in the `!secret` values:
   - `LLM_API_KEY` — your OpenAI-compatible endpoint key
   - `NTFY_TOKEN`, `HA_TOKEN`, `SIP_PASSWORD`, etc. — as needed for enabled plugins

3. Edit `instance/config.yaml` to enable/configure plugins. The full reference is `ARES-SPEC.md` §8. Key plugin flags (most default to `enabled: false`):
   - `cli` — CLI source for development
   - `console_channel` — prints replies to the terminal (on unless set to `false`)
   - `scheduler` — time-based events and task reminders (enabled by default)
   - `push_ntfy` — push notifications via ntfy
   - `home_assistant` — Home Assistant integration
   - `speaker` — announce replies on a Home Assistant speaker when someone is home (needs `home_assistant`)
   - `voice` — voice pipeline (speech recognition + TTS)
   - `sip` — SIP calling and messaging
   - `time_tools` — weather, calendar (read/add)
   - `safety_critical` — deterministic handlers for fire/intrusion (bypasses LLM)
   - `shell` — sandboxed command execution
   - `browser` — `fetch_page` and the persistent, operator-supervised `browser` (needs Chromium; prod needs `browser_user`)
   - `subagents` — bounded background runs that report back
   - `privileges` — privilege escalation queue (requires broker)
   - `dashboard` — web interface
   - `selfedit` — self-modification (branch, PR, human-gate merge)

Secrets are resolved from `.env` at startup via the `!secret KEY` syntax in YAML. **Never put secrets in code or config files**—only `.env`.

## Running

Start the daemon with a config path (defaults to `instance/config.yaml`):

```bash
python instance/main.py [config_path]
```

### CLI (Development Surface)

When the CLI source is enabled, you can interact live:

```
Type a line to send a NORMAL-priority event.
ARES> hello
ARES> I can do this task in 5 minutes

Shortcuts:
  !high <text>     Send a HIGH-priority event (urgent)
  !quit            Exit cleanly
```

ARES replies via the `speak` tool:
```
ARES> I understand. I'll check the oven timer.
```

## Testing

Run the test suite:

```bash
.venv/bin/pytest tests/ -q
```

Tests cover the core (tool registry search, memory path safety, task store, config, sessions, dispatcher policy, the agent loop), the security invariants (RULES sync, sandbox/browser user separation, the egress proxy, broker allowlist, updater HMAC and SHA checks, SIP caller matching), and the dashboard routes. Hardware-dependent plugins (voice, SIP audio) have import-level and config tests only.

## Architecture (Brief Overview)

ARES is built around **an async event bus** and a **priority model**:

- **CRITICAL** events (fire, intruder) skip the LLM entirely, triggering deterministic handlers instantly.
- **HIGH** events (time-sensitive commands) get priority queuing.
- **NORMAL** events (conversation, reminders) flow through the agent's LLM loop.
- **LOW** events are dropped when the dispatcher is busy, preventing queue buildup.

**One session per user** persists across all channels, so a conversation can start on voice, pause, and continue over text—the agent retains context. The **ResponseRouter** decides *where* to send a reply based on the channel the user last spoke on; if that channel can't deliver (e.g. the dashboard tab is closed) it falls back to a Home Assistant speaker when someone is home, then push, then the console. The agent loop has a tool-call budget; it reasons via the LLM, discovers domain-specific tools with `search_tools`, and executes them.

Work that would hold up the conversation goes to **background subagents**: bounded, read-only LLM loops that report back as events. Every agent turn is written to a live **activity trace** the dashboard can tail.

**Memory** is markdown files in `instance/memory/`—human-readable, editable, and searched by grep. The LLM maintains an `INDEX.md` file listing where facts live. **Tasks** live in SQLite (`instance/tasks/tasks.db`); open tasks are injected into every agent cycle so the daemon keeps intent and continuity across restarts.

See `ARES-SPEC.md` §2 and §4 for full architecture, session management, and core framework detail.

## Adding a Plugin

Plugins extend ARES with sources (event inputs), channels (delivery transports), tools (agent capabilities), or critical handlers (deterministic fast paths).

### Source

Subclass `ares.core.source.BaseSource`:

```python
from ares.core.source import BaseSource
from ares.core.event import EventBus, Priority
from ares.core.config import ConfigError

class MySource(BaseSource):
    name = "my_source"  # class attribute, unique identifier
    
    def __init__(self, bus: EventBus, config: dict) -> None:
        """Initialize with bus and config; validate config, raise ConfigError if invalid."""
        super().__init__(bus, config)
        self.enabled = config.get("enabled", False)
        if not isinstance(self.enabled, bool):
            raise ConfigError("my_source: 'enabled' must be bool")
    
    async def start(self) -> None:
        """Run until return (intentional stop) or raise (will restart with backoff)."""
        while True:
            # ... listen for events ...
            await self.emit(
                type="some_event",
                payload={"key": "value"},
                priority=Priority.NORMAL
            )
```

Register in `instance/main.py` in the wiring section.

### Channel

Subclass `ares.core.channel.BaseChannel`:

```python
from ares.core.channel import BaseChannel, ChannelType
from ares.core.session import Session

class MyChannel(BaseChannel):
    type = ChannelType.CONSOLE  # one of: VOICE, SIP_CALL, SIP_MESSAGE, SPEAKER, PUSH, CONSOLE, WEB
    
    async def deliver(self, user_id: str, message: str, session: Session) -> bool:
        """Send message to the user. Return True if sent, False if unavailable."""
        # ... send message ...
        return success
```

Register on the `ResponseRouter` in `instance/main.py`.

### Tool

Subclass `ares.core.tool.BaseTool`:

```python
from ares.core.tool import BaseTool, ToolContext, ToolResult

class MyTool(BaseTool):
    name = "my_tool"
    description = "What this tool does."
    keywords = ("keyword", "list")  # tuple of keyword strings
    core = False  # True if always in context; False if discoverable via search_tools
    parameters = {
        "type": "object",
        "properties": {
            "arg": {"type": "string", "description": "..."}
        },
        "required": ["arg"]
    }
    
    async def run(self, ctx: ToolContext, **kwargs) -> ToolResult:
        """Execute the tool."""
        # ctx.services["plugin_name"] accesses plugin services
        # e.g., ctx.services["home_assistant"].turn_on(entity)
        return ToolResult(ok=True, content="Result")
```

Register in `instance/main.py` via `ToolRegistry.register()`.

### Plugin Services

If your plugin needs shared state (e.g., an API client), place it in `services["plugin_name"]` and access it from tools via `ctx.services["plugin_name"]`.

### Key Rules

- Plugins import from `ares.core` only; core never imports from plugins.
- Sources call `self.emit()` with `type`, `payload`, and `priority` to publish; the event bus routes automatically.
- Channels' `deliver()` method returns `bool`; if False, ARES tries the fallbacks (SPEAKER, then PUSH, then CONSOLE).
- Plugins never import each other; a collaborator from another plugin arrives through `services`.
- Tool exceptions are logged; return `ToolResult(ok=False, content="error")` for user-facing failures.
- All I/O is async (`asyncio`). No threads except where a library forces it.

See `ares/plugins/` for working examples of sources, channels, and tools.

## Development Workflow

1. **Read** `PROGRESS.md` to see what's done and where to resume.
2. **Activate the venv** and verify: `which python`.
3. **Make changes** to code in `ares/`. Spec changes need operator sign-off, a version bump, and an `ARES-SPEC.md` Appendix A entry.
4. **Run tests** before committing: `.venv/bin/pytest tests/ -q`. If you touched the dashboard frontend, rebuild it with `.venv/bin/python ares/plugins/dashboard/frontend/build.py`.
5. **Update** `PROGRESS.md` (short `## Current`, full entry under `## History`) and commit it with the change: `<version or kind>: <what> — <summary>`.

See `CLAUDE.md` for detailed working rules.

## Deployment

This README is for developers and operators running locally or in a homelab. For production deployment in a Firecracker microVM with privilege separation, security boundaries, and automated updates, see `DEPLOYMENT.md`.
