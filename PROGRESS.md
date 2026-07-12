# ARES Build Progress

Spec: `ARES-SPEC.md` v1.1. Read spec §0 (rules) before every session. This file
is the single source of truth for build state. Protocol: spec §11. The
deployment/security layer is M10–M13; read spec §14 before touching any of it.

## Current

Milestone: M13 (M12 complete, acceptance passed — real local-git origin: open_pr branches +
commits into scratch clone only + pushes, live_code_path never written, path-escape rejected,
get_pr_status reads state, opened PR shows in dashboard /api/prs over real HTTP (token-gated),
and structurally ARES has no merge tool/route; suite 180 green). Open items carried forward:
M6 live-HA-WS Blocker; live voice/SIP need hardware/PJSIP. NOTE: instance/main.py is at
400/400 lines — M13 barely touches it (tripwires live in config.py/secrets.py/shell_tools.py),
but any main.py addition MUST reclaim first. Next action: begin M13 (Update listener + deploy
artifacts + prod tripwires) — READ spec §19 (done) and §14 FIRST. HARD security rules:
updater/aresupdater.py is STDLIB-ONLY and must NEVER import `ares` (test asserts this, like
the broker); webhook verifies X-Hub-Signature-256 HMAC vs ARES_WEBHOOK_SECRET; poll fallback
via git ls-remote; safe swap = temp worktree + smoke-import + ABORT-on-failure (never swap a
broken tree), serialised with a lock; restart via sudo systemctl (fixed argv). Prod tripwires:
prod fails fast on a readable .env (secrets must come from os.environ) and on missing sandbox
user separation (shell/selfedit refuse own-uid — already partly done). Do NOT weaken prod
tripwires to pass a test; fix the test setup.

## Ticklist

### M0 — Skeleton
- [x] pyproject.toml  (core deps only; extras: voice, sip, calendar, dev — spec §12)
- [x] .gitignore  (.venv/, .env, instance/tasks/*.db, instance/privq.db, broker.json, updater.json, scratch/, __pycache__, *.pyc, .pytest_cache)
- [x] .venv created (`python3.11 -m venv .venv`) and `.venv/bin/pip install -e ".[dev]"` succeeds — CLAUDE.md venv rules apply from here on
- [x] ares/__init__.py
- [x] ares/core/__init__.py
- [x] ares/core/utils/__init__.py
- [x] ares/core/utils/logging.py
- [x] ares/core/utils/ids.py
- [x] ares/core/utils/text.py
- [x] ares/core/secrets.py
- [x] ares/core/config.py  (full Pydantic model tree per spec §8)
- [x] instance/.env.example  (every !secret key from spec §8)
- [x] instance/config.yaml  (copy of spec §8 example)
- [x] instance/memory/INDEX.md  (verbatim from spec §9)
- [x] instance/memory/long-term/preferences.md
- [x] instance/memory/long-term/people.md
- [x] instance/memory/long-term/routines.md
- [x] instance/memory/long-term/home.md
- [x] instance/memory/short-term/.gitkeep
- [x] instance/tasks/.gitkeep
- [x] tests/test_config.py  (!secret resolution, SecretNotFound, unknown top-level key rejected)
- [x] M0 acceptance test passed (spec §10)

### M1 — Bus + CLI loop (no LLM)
- [x] ares/core/event.py
- [x] ares/core/source.py
- [x] ares/core/channel.py
- [x] ares/core/session.py
- [x] ares/core/router.py
- [x] ares/core/critical.py
- [x] ares/core/dispatcher.py
- [x] ares/plugins/__init__.py
- [x] ares/plugins/sources/__init__.py
- [x] ares/plugins/sources/cli.py
- [x] ares/plugins/channels/__init__.py
- [x] ares/plugins/channels/console.py
- [x] instance/main.py  (wiring + source supervisor + echo agent stub)
- [x] tests/test_session.py  (timeout clears history, history trim, touch channel mapping)
- [x] tests/test_dispatcher.py  (per-user serialisation, LOW drop when busy, HIGH front-of-queue)
- [x] M1 acceptance test passed (echo over CLI, !quit clean exit)

### M2 — Real agent
- [x] ares/core/llm/__init__.py
- [x] ares/core/llm/client.py
- [x] ares/core/tool.py
- [x] ares/core/prompt.py
- [x] ares/core/agent.py
- [x] ares/plugins/tools/__init__.py
- [x] ares/plugins/tools/core_tools.py  (all six core tools, spec §5)
- [x] instance/main.py updated (real Agent replaces echo stub)
- [x] tests/test_tool_registry.py  (search scoring, core/non-core, to_oai_schema)
- [x] tests/test_agent.py  (mocked LLM: tool loop, budget exhaustion, forced speak on user input)
- [x] M2 acceptance test passed (live conversation over CLI via speak tool)

### M3 — Memory
- [x] ares/core/memory/__init__.py
- [x] ares/core/memory/base.py
- [x] ares/core/memory/filesystem.py
- [x] ares/plugins/tools/memory_tools.py
- [x] tests/test_memory.py  (path escape rejected, non-.md rejected, grep truncation, append vs overwrite, prune_short_term)
- [x] instance/main.py updated (real FilesystemMemory replaces _StubMemory; MEMORY_TOOLS registered)
- [x] M3 acceptance test passed (remember → new session → recall via grep)

### M4 — Tasks + scheduler
- [x] ares/core/tasks/__init__.py
- [x] ares/core/tasks/store.py
- [x] ares/plugins/tools/task_tools.py
- [x] ares/plugins/sources/scheduler.py
- [x] tests/test_task_store.py  (CRUD, list_due boundary, close sets resolution/closed_at, type CHECK)
- [x] instance/main.py updated (real TaskStore replaces _StubTasks; scheduler source + task tools wired)
- [x] M4 acceptance test passed (2-minute reminder fires as HIGH task_due, spoken, closed)

### M5 — Sessions & routing across channels
- [x] ares/plugins/channels/push_ntfy.py
- [x] tests/test_router.py  (delivery-time channel read, PUSH fallback on deliver False, notify path)
- [x] instance/main.py updated (register NtfyChannel when push_ntfy enabled)
- [x] M5 acceptance test passed (channel switch mid-conversation, fallback verified)

### M6 — Home Assistant
- [x] ares/plugins/sources/home_assistant.py  (source + HAService) — WS transport BLOCKED (see Blockers); filter/service fully built
- [x] ares/plugins/tools/home_tools.py
- [x] ares/plugins/critical/__init__.py
- [x] ares/plugins/critical/safety.py
- [x] instance/main.py updated (service registration into services dict)
- [x] tests/test_ha_filter.py  (domain allow-list, same-state drop, debounce, priority rules mapping)
- [x] M6 acceptance test passed (filtered events, device control, CRITICAL bypass with no LLM call) — against MOCKED HA; live WS transport still Blocked

### M7 — Voice
- [x] ares/plugins/sources/voice/__init__.py
- [x] ares/plugins/sources/voice/vad.py
- [x] ares/plugins/sources/voice/stt.py
- [x] ares/plugins/sources/voice/intent.py
- [x] ares/plugins/sources/voice/source.py
- [x] ares/plugins/channels/voice_tts.py
- [x] instance/main.py updated (wire voice sources + VoiceTTSChannel when voice.enabled)
- [x] tests/test_voice_config.py  (import-level + config validation only, per spec §10)
- [x] M7 acceptance test passed (import-level + config validation, per spec §10; live-audio behavior needs hardware + voice extra)

### M8 — SIP
- [x] ares/plugins/sip/__init__.py
- [x] ares/plugins/sip/client.py
- [x] ares/plugins/sip/source.py
- [x] ares/plugins/channels/sip_message.py
- [x] ares/plugins/channels/sip_call.py
- [x] ares/plugins/tools/comms_tools.py
- [x] instance/main.py updated (wire SIP service/source/channels/tools when sip.enabled)
- [x] tests/test_sip_config.py  (import-level + config validation only)
- [x] M8 acceptance test passed (import-level + config validation, per spec §10; live SIP needs PJSIP + Asterisk)

### M9 — Time tools + hardening
- [x] ares/plugins/tools/time_tools.py
- [x] instance/main.py updated (register time tools when time_tools.enabled)
- [x] Source supervisor restart/backoff verified (tests/test_supervisor.py: clean-return, restart-then-success, max-10→source_failed, CancelledError propagates)
- [x] Graceful shutdown audit (Ctrl-C exits < 5 s, tasks cancelled, DB closed) — hardened: cancellable async stdin reader + SIGINT/SIGTERM handlers; SIGINT now exits ~0.1s
- [x] README.md  (install incl. Piper/PJSIP, config, running, adding a plugin)
- [x] M9 acceptance test passed (weather e2e vs LIVE Open-Meteo, restart observed, clean shutdown)

### M10 — Sandboxed shell + privilege queue + broker  (read spec §14, §15, §16 first)
- [x] ares/plugins/tools/shell_tools.py  (run_shell; runs as ares-sbx in prod, never as ares)
- [x] ares/plugins/privileges/__init__.py
- [x] ares/plugins/privileges/store.py  (PrivStore; approve/deny NOT on tool surface)
- [x] ares/plugins/privileges/tools.py  (request_privilege, get_privilege_requests)
- [x] ares/plugins/privileges/source.py  (poll for decided requests -> privilege_update events)
- [x] broker/aresbrokerd.py  (STDLIB ONLY, no ares import, <200 lines, allowlist re-validate)
- [x] broker/broker.example.json
- [x] instance/main.py updated (register privileges plugin + PrivStore service + shell tool)
- [x] tests/test_priv_store.py  (state machine, approve/deny)
- [x] tests/test_broker.py  (allowlist accept/reject, argv build never shell=True, no ares import)
- [x] tests/test_shell.py  (timeout cap, non-zero exit is ok=True, refuses own-uid in prod)
- [x] M10 acceptance test passed (spec §10) — dev mode: run_shell+warning, request_privilege→pending, broker executes allowlisted install→done, non-allowlisted rejected, privilege_update reaches agent

### M11 — Web dashboard  (read spec §17 first)
- [x] ares/plugins/dashboard/__init__.py
- [x] ares/plugins/dashboard/channel.py  (WebChannel + per-user outbox queues)
- [x] ares/plugins/dashboard/api.py  (FastAPI routes, token auth, thin wrappers)
- [x] ares/plugins/dashboard/server.py  (DashboardSource, uvicorn in-loop)
- [x] ares/plugins/dashboard/static/index.html  (single-file vanilla JS UI)
- [x] instance/main.py updated (register dashboard source + WebChannel)
- [x] tests/test_dashboard.py  (token auth required, memory path safety, approve flips DB)
- [x] M11 acceptance test passed (chat round-trip, approvals, RO memory)

### M12 — Self-edit -> PR  (read spec §18 first)
- [x] ares/plugins/tools/selfedit_tools.py  (open_pr, get_pr_status; scratch repo ONLY)
- [x] instance/main.py updated (register selfedit tools)
- [x] tests/test_selfedit.py  (path escape in files rejected, never writes live code path)
- [x] M12 acceptance test passed (PR opened against test repo, cannot merge, status read)

### M13 — Update listener + deploy artifacts  (read spec §14, §19 first)
- [x] updater/aresupdater.py  (STDLIB ONLY, no ares import; webhook HMAC + poll + safe swap)
- [ ] deploy/provision.sh  (users, dirs, perms, sudoers, unit install; passes shellcheck + bash -n)
- [ ] deploy/ares.service
- [ ] deploy/ares-broker.service
- [ ] deploy/ares-updater.service
- [x] ARES_ENV prod tripwires in config.py / secrets.py / shell_tools.py (fatal on readable .env, missing sandbox user)
- [x] tests/test_updater.py  (HMAC verify accept/reject, smoke-import-abort, no ares import)
- [x] tests/test_prod_tripwire.py  (prod mode fatal on misconfig)
- [ ] M13 acceptance test passed (poll detects SHA, swap+restart, webhook verify, tripwire fires)

## Decisions

- 2026-07-11 (M0): pyproject includes the `dashboard` extra (fastapi, uvicorn) in
  addition to voice/sip/calendar/dev. Spec §12 lists dashboard as an extra; the M0
  ticklist parenthetical omitted it. §12 is authoritative.
- 2026-07-11 (M0): venv built on python3.12 (3.12.3); spec/CLAUDE.md require ">=3.11".
- 2026-07-11 (M0): setuptools `packages = ["ares"]` (explicit) rather than find-based
  discovery; editable install resolves all `ares.*` subpackages via the package path,
  and the project is only ever installed editable in dev.
- 2026-07-11 (M0): spec §8 line 899 has `living_room:{` (no space after the colon),
  which is invalid YAML and makes the reference config unparseable. Fixed in our
  `instance/config.yaml` copy to `living_room: { ... }` (matching the `kitchen:` line's
  valid flow-mapping style) so M0 acceptance can load it. Spec file itself untouched.
- 2026-07-11 (M1): added `class ConfigError(Exception)` to `ares/core/config.py`.
  §7 says sources raise `ConfigError` from `core.config`, but §4.7 (M0) never listed
  it, so it was added when the source layer began (not a refactor — a required symbol).
- 2026-07-11 (M1): §4.4 shows no `__init__` for `CriticalHandlerRegistry`, but its
  `handle(event)` must hand a `ResponseRouter` to each handler. Reconciled by giving
  the registry `__init__(self, router)` (main.py injects the router).
- 2026-07-11 (M1): §4.3 froze `Dispatcher.__init__(bus, agent, critical)` yet §4.6
  requires the dispatcher to `touch` sessions before the agent. Reconciled: the
  dispatcher reaches the SessionManager via `agent.sessions` (both the M1 echo stub
  and the M2 real Agent expose `.sessions`). `Agent` is a TYPE_CHECKING-only import in
  dispatcher.py (agent.py arrives in M2), so the module imports cleanly now.
- 2026-07-11 (M1): main.py uses `EnvSecretStore(instance/.env)` if present else falls
  back to `instance/.env.example` so the daemon runs in dev without a real .env. The
  M13 prod tripwire will forbid reading a readable .env in prod.
- 2026-07-11 (M2): the Agent's frozen §4.10 signature requires a `TaskStore` (M4) and
  `BaseMemory` (M3), which are later ticklist items. main.py wires minimal `_StubTasks`
  (`list_open`→[], other methods raise) and `_StubMemory` (methods raise) so the
  speak-path acceptance runs. M3 and M4 each include a main.py update to swap in the
  real FilesystemMemory / TaskStore. Stubs live only in the wiring file; no new core
  abstractions. tasks/memory tools are not registered in M2 (registry = core tools only).
- 2026-07-11 (M2): agent.py reads the session via `sessions.get` (not a second `touch`),
  because the Dispatcher already applied the §4.6 channel-mapping touch immediately
  before calling `handle` — avoids duplicating the mapping table across two core modules.
- 2026-07-11 (M2): M2 acceptance (§10 "live OAI endpoint") verified against a local
  stdlib mock OAI server (the configured ollama.local is not reachable in this env),
  exercising the real LLMClient→Agent→CLI→router path end-to-end; plus mocked-LLM unit
  tests in test_agent.py. A real-endpoint run is available to the operator via config.
- 2026-07-11 (M7): the `voice` extra (faster-whisper, silero-vad, openwakeword,
  sounddevice, numpy) is NOT installed in this env (torch/ctranslate2 resolution is
  heavy and there is no audio hardware). Per spec §10 the M7 test is import-level +
  config validation ONLY. So the voice modules use GUARDED/LAZY imports of the heavy
  libs (top-level `try/except ImportError` → `_HAVE_*` flag; the wrapper classes raise
  a clear RuntimeError at INSTANTIATION if the lib is missing). This lets the modules
  import and validate config without the extra installed (satisfying §10), while real
  audio operation requires `pip install -e ".[voice]"` on hardware. Not a Blocker —
  M7 is fully implementable/testable at the spec-mandated level.
- 2026-07-11 (M8): the `sip` extra is `pjsua2` (a system PJSIP build), NOT installable
  here. Same approach as M7: SIP modules use a guarded `pjsua2` import; `SIPService`
  raises RuntimeError on instantiation if pjsua2 is absent; the channels/source/tools
  drive an injected SIPService and import/validate config without pjsua2. The pjsua2-
  using bodies of SIPService (register/send_message/call_and_speak/speak_into_call) are
  structurally correct but cannot be exercised in this env (no PJSIP, no SIP server), so
  the M8 acceptance is import-level + config validation per §10. Live SIP (MESSAGE
  round-trip, inbound call) needs a real PJSIP build + Asterisk. Not a Blocker — M8 is
  fully implementable/testable at the spec-mandated level. SIPService gained a
  `user_uris: dict[str,str]` field + `has_active_call()`/`speak_into_call()` helpers
  (spec §7.5 shows the core methods; these are the minimal glue the channels/tools need).
- 2026-07-12 (M11): the dashboard config key is `password` (`!secret DASHBOARD_PASSWORD`,
  per config.yaml + spec §17: "single shared password ... sent as a bearer token").
  `DashboardSource` reads `config["password"]` and uses it verbatim as the bearer token;
  `api.build_app(token=...)` compares with `hmac.compare_digest`. No separate `token` key.
- 2026-07-12 (M11): `DashboardSource.start()` BLOCKS on `uvicorn.Server.serve()` (not a
  fire-and-forget task) so the §4.2 supervisor supervises it directly — a uvicorn crash
  propagates to `supervise()` for restart; `stop()` sets `should_exit` so serve() returns
  and supervision ends cleanly. Matches the scheduler/sip `while not _stopping` convention.
- 2026-07-12 (M11): trimmed a 3-line comment in main.py to keep it at 398/400 after adding
  dashboard wiring. M12 wiring will need further reclamation or the wiring must move.
- 2026-07-12 (M12): no real GitHub/token reachable here, so the M12 acceptance (and
  test_selfedit) exercise the git half against a LOCAL bare repo used as `origin` (proving
  branch/commit-into-scratch-only/push + live-never-written + path-escape rejection), and
  mock ONLY the GitHub REST seams `OpenPR._create_pr` / `GetPRStatus._fetch`. This is the
  §10-mandated level here; live PR creation needs a real repo + fine-grained PAT + branch
  protection (operator, per DEPLOYMENT.md). "ARES cannot merge" is verified structurally
  (no merge tool, no /merge route) — branch protection is the real external gate.
- 2026-07-12 (M12): hardened selfedit `_run_git` to scrub the GitHub token from all git
  output before it is returned in a ToolResult/log (a failed clone/fetch/push can otherwise
  echo the token-embedded `x-access-token:TOKEN@github.com` origin URL). Not explicit in the
  spec but required by the secret-handling rules (§14 / CLAUDE.md).
- 2026-07-12 (M12): trimmed 5 self-evident inline comments in main.py so the selfedit +
  prs_provider wiring keeps it at exactly 400/400 (spec §0.10 hard cap). `DashboardSource`
  gained a trailing optional `prs_provider` param (defaults to `lambda: []`).

## Blockers

- 2026-07-11 (M6): **HA live WebSocket transport.** §7.3 requires `HomeAssistantSource`
  to connect to `ws_url`, authenticate, and `subscribe_events(state_changed)` over a
  WebSocket. §12's dependency list contains NO WebSocket client (core: pydantic, PyYAML,
  python-dotenv, httpx, aiosqlite; httpx has no WS support), and §0 forbids adding
  dependencies. Hand-rolling an RFC6455 client would be inventing unspecified plumbing
  (and untestable without a WS server dep). RESOLUTION PENDING A USER DECISION: add a WS
  dependency (e.g. `websockets`) to §12, or accept REST polling.
  MITIGATION (so M6 still ships & the acceptance passes against a MOCKED HA, per §10):
  everything except the live WS wire is built and tested — HAService REST methods
  (get_state/get_states/call_service/snapshot_summary/camera_snapshot over httpx), the
  full noise filter (domain/entity allow-list, same-state drop, debounce, priority-rule
  mapping) exposed via `HomeAssistantSource.process_state_changed(...)` and driven by
  synthetic events, `ares_event` passthrough, home_tools, and critical/safety handlers.
  `start()` logs this blocker and idles (the source is inert at runtime until a WS
  transport is provided); it does NOT crash the daemon.
