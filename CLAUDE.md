# CLAUDE.md — Working Rules for This Repository

You are implementing ARES from `ARES-SPEC.md`. That spec is authoritative and
frozen. This file tells you how to work, not what to build.

## Session start (every session, no exceptions)

1. Read `ARES-SPEC.md` §0 (implementer rules) in full.
2. Read `PROGRESS.md` — the `## Current` section tells you exactly where to
   resume. Do not re-derive state from the codebase.
3. Read only the spec sections relevant to the current milestone (the ticklist
   entries reference them). Do not re-read the whole spec every session. For any
   M10–M13 work, read spec §14 (security model) first — it constrains all of it.
   `DEPLOYMENT.md` is the operator's guide, **not** an implementation input;
   read it only to understand the runtime your code lands in, never build from it.
4. Activate the virtual environment (see "Virtual environment" below) and
   verify with `which python` before running anything.
5. Run `git status` and `git log --oneline -5` to confirm the working tree is
   clean and matches PROGRESS.md. If the tree is dirty or they disagree,
   reconcile (commit or revert) before writing any new code.

## Virtual environment

All Python execution happens inside the repo-local venv at `.venv/`. No
exceptions — not for tests, not for one-liner import checks, not for pip.

- If `.venv/` does not exist (first M0 session), create it:
  `python3.11 -m venv .venv` (or newer), then
  `.venv/bin/pip install -e ".[dev]"`.
- At the start of every session: `source .venv/bin/activate`, then confirm
  `which python` prints a path ending in `.venv/bin/python`. If it doesn't,
  stop and fix that before anything else.
- If activation is unreliable in your shell environment, call the binaries by
  explicit path instead: `.venv/bin/python`, `.venv/bin/pip`,
  `.venv/bin/pytest`. Never bare `python`, `pip`, or `pytest` unless you have
  just verified they resolve into `.venv/`.
- Never `pip install` outside the venv, never use `sudo pip`, never
  `pip install --user`, never touch the system Python.
- Installs are only `pip install -e ".[<extra>]"` with extras defined in
  `pyproject.toml` (spec §12). Installing a package not in `pyproject.toml`
  is forbidden — that's a spec §0 dependency violation.
- `.venv/` is gitignored and never committed. If you find it staged,
  unstage it and fix `.gitignore` first.
- If the venv breaks, delete and recreate it from `pyproject.toml`. Do not
  hand-patch site-packages.

## Reading files

- Never assume a file's contents from memory — read it before editing it.
- Before implementing a module, read the interfaces it depends on
  (e.g. read `event.py` before writing `dispatcher.py`). Dependencies flow
  strictly: plugins → core; core never imports plugins.
- Do not open or read `.env`. Use `.env.example` to see which keys exist.
- If a file you expect to exist is missing, check PROGRESS.md — it is probably
  a later ticklist item. Do not create it early.

## Order of work

- Follow the PROGRESS.md ticklist top to bottom. One item at a time.
- Do not start a milestone until the previous milestone's acceptance test
  passes and is ticked.
- Do not refactor completed modules while working on later milestones.
- Spec silent on a detail → simplest option that passes the acceptance test,
  plus a dated line under `## Decisions`.
- Missing dependency, spec contradiction, or anything needing a spec change →
  a line under `## Blockers`, then move to the next unblocked item. Never
  work around a blocker by adding dependencies or inventing architecture.

## After completing each file

1. Run the relevant tests (`.venv/bin/pytest tests/ -x -q`). New core code
   without a listed test still must import cleanly:
   `.venv/bin/python -c "import ares.core.<mod>"`.
2. Update PROGRESS.md: tick the item, update `## Current` to the next action.
3. Commit (see below). The PROGRESS.md update goes in the same commit as the
   code it describes.

## Git rules

- Commit after every ticklist item. Small commits, one item each.
- Message format: `M<milestone>: <path or item> — <one-line summary>`
  - `M2: ares/core/agent.py — event handling cycle per spec §4.10`
  - `M4: acceptance — reminder fires and closes end-to-end`
- Milestone completion gets its own commit: `M3: milestone complete — acceptance passed`.
- Never commit: `.env`, `instance/tasks/*.db`, `instance/privq.db`, `.venv/`,
  `/etc/ares/*` real configs, `broker.json`, `updater.json`, `__pycache__`,
  model weights, audio/temp files, the scratch clone. Only `.example` config
  variants are committed. Check `.gitignore` covers anything new you generate.
- Never use `git add .` — stage the specific files you touched.
- No amending or rebasing published history. If a committed file was wrong,
  fix it in a new commit: `fix(M2): agent.py — <what was wrong>`.
- Work on `main` directly. No branches, no merge commits (single-agent repo).
- Never commit a broken tree: if tests fail, fix or revert before committing.

## Security boundaries (M10–M13 — do not blur these)

These are the point of the deployment layer. Breaking one silently is worse than
not building the feature. Read spec §14 before writing any M10–M13 code.

- `broker/` and `updater/` are **stdlib-only** and must **never import `ares`**
  (there are tests that assert this). They run at higher privilege; keep them
  tiny and dependency-free.
- ARES code **never** approves privilege requests and **never** merges its own
  PRs. `approve`/`deny` are dashboard-operator actions; merge is a human GitHub
  action gated by branch protection. There must be no code path from ARES's
  reasoning to running privileged or self-modified code without a human gate.
- The shell tool runs as the sandbox user (`ares-sbx`), **never** as the daemon
  user. In prod, if the runner would be the `ares` uid, refuse and log.
- The daemon never writes `/opt/ares` (its own live code) and never reads the
  secret `.env` file. Secrets come from `os.environ` only. Self-edits touch the
  scratch clone only.
- The broker executes only requests that are **both** human-approved **and**
  allowlist-regex-matched. Build argv from fixed templates — never `shell=True`,
  never split attacker-controlled strings.
- Respect `ARES_ENV`: dev may relax (single user, dotenv, scratch dirs) with
  warnings; prod must fail fast on any missing separation. Do not weaken the
  prod tripwires to make a test pass — fix the test setup instead.
- The system-prompt RULES block (spec §4.11 / `ARES-SYSTEM-PROMPT.md`) is a fixed
  code constant and carries the injection defenses. It must be reproduced
  verbatim, must never be sourced from or overridable by config, and the config
  persona is only ever concatenated before it. Non-user events must be rendered
  fenced as `[EVENT ...]` so external content cannot pose as a user turn.

## Hard don'ts (repeated from spec §0 because they matter)

- No new dependencies beyond spec §12.
- No vector DBs, embeddings, LangChain, or agent frameworks.
- No features outside the v1 scope list (spec §1) — the out-of-scope list is
  binding.
- No editing `ARES-SPEC.md`. If the spec is wrong, that is a Blocker.
- Never commit `broker.json`, `updater.json`, or any real token/secret — only
  the `.example` variants.
