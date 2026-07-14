# ARES — Deployment Guide (Operator)

This is **your** guide, not the coding agent's. It describes every component you
need to stand up to run ARES safely in a Firecracker microVM, in the order you
should build them. The coding agent produces the software (per `ARES-SPEC.md`);
this document is how you host it.

The security model in one sentence: **ARES runs unprivileged, cannot read its
own secrets, cannot edit the code it runs, and can only reach root or modify
itself through queues that you approve.** Everything below exists to make that
true. If you shortcut it, you lose the guarantee.

---

## 0. Topology

```
        your LAN / Tailnet
┌──────────────────────────────────────────────┐
│  HOST (bare metal / hypervisor)               │
│   ├─ Firecracker microVM  ── "ares-vm"        │
│   │    ├─ ares          (daemon, no sudo)     │
│   │    ├─ ares-sbx      (sandbox shells)      │
│   │    ├─ ares-deploy   (update listener)     │
│   │    └─ root          (broker)              │
│   ├─ Ollama / vLLM      (LLM inference)       │  ← can be host or another box
│   ├─ Home Assistant                           │
│   ├─ Asterisk           (SIP)                 │
│   └─ ntfy               (push)                 │
└──────────────────────────────────────────────┘
        ▲ Tailscale for remote access
        ▲ GitHub webhook via reverse tunnel → updater
```

You can start with everything on the host and only ARES in the VM. That's fine —
the VM boundary that matters is the one around ARES.

---

## 1. Host prerequisites

- A Linux host with KVM (`/dev/kvm` present) for Firecracker.
- `firecracker` and `jailer` binaries (grab a release from the
  firecracker-microvm project).
- A guest kernel (`vmlinux`) and a root filesystem image (ext4) for the VM.
  Ubuntu 24.04 minimal or Debian 12 is a good base.
- Host networking: a TAP device bridged/NAT'd so the VM has outbound access and
  you can reach its ports. A `/30` TAP with NAT is the simplest.
- Tailscale on the host (or in the VM) for remote access to the dashboard and
  SIP without exposing ports to the internet.

Firecracker itself is out of scope for the ARES code — you provide the VM; ARES
just runs inside it. Keep your VM config (`vmconfig.json`: kernel, rootfs,
vCPUs, mem, TAP) in your own infra repo, not the ARES repo.

---

## 2. Build the VM image

1. Boot a base rootfs, or mount and chroot the ext4 image.
2. Install runtime deps inside it:
   - `python3.11`+, `python3-venv`, `git`, `grep`, `sudo`, `systemd`.
   - Piper binary + a voice model (only if using voice inside the VM; usually
     voice hardware lives elsewhere and reaches ARES over the network, so you
     may skip audio libs in the VM).
   - PJSIP + `pjsua2` bindings only if SIP runs in-VM.
3. Copy the ARES repo release to `/opt/ares/releases/<sha>` and symlink
   `/opt/ares/app -> /opt/ares/releases/<sha>`.
4. Create the app venv: `python3.11 -m venv /opt/ares/venv` and
   `/opt/ares/venv/bin/pip install -e "/opt/ares/app[dashboard]"` (add
   `,voice`, `,sip`, `,calendar` extras as needed).
5. Run the provisioning script (next section).

Rebuild the image whenever you change base packages; ARES **code** updates do
not need an image rebuild — that's what the update listener is for (§7).

---

## 3. Run `deploy/provision.sh` inside the VM

This script (shipped in the repo, written by the coding agent per spec §14) is
idempotent and does all the privilege setup. Review it before running — it is
the thing that creates the security boundary. It:

- Creates users: `ares`, `ares-sbx`, `ares-deploy` (all no-login shells except
  as needed), leaving `root` for the broker.
- Creates and `chown`/`chmod`s:
  - `/opt/ares` → `ares-deploy:ares`, app tree `0750` (RO to `ares`).
  - `/etc/ares/config.yaml` → `0640 root:ares`.
  - `/etc/ares/.env` → `0600 root:root` (**ARES cannot read this**).
  - `/etc/ares/broker.json`, `/etc/ares/updater.json` → `0600 root:root`.
  - `/var/lib/ares` → `0700 ares:ares` (state, memory, DBs).
  - `/home/ares-sbx/scratch` → `ares-sbx` (sandbox scratch for `run_shell`;
    self-edit no longer uses a clone — it is API-only from the daemon).
- Installs `deploy/sbx-runner` to **`/usr/local/sbin/ares-sbx-runner`**
  (`root:root`, `0755`) — the sole sudo entry point `ares → ares-sbx`. It is
  installed **outside the app tree deliberately**: it is therefore not part of
  the self-edit surface, so ARES cannot weaken its own sandbox entry point via a
  merged PR — changing it requires re-running `provision.sh`, a deliberate
  operator action. The runner scrubs the environment (`env -i`) so the daemon's
  secret env can never reach a sandbox shell, regardless of sudoers config.
- Installs the **sudoers drop-ins**, each a single exact rule:
  - `ares ALL=(ares-sbx) NOPASSWD: /usr/local/sbin/ares-sbx-runner` — lets the
    daemon drop **down** to the sandbox user to run shells, via the runner only.
    Not escalation.
  - `ares-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart ares` — lets
    the updater restart the daemon after a verified update. Nothing else.
  - **No sudoers rule gives `ares` any root.** Root actions go through the
    broker, which runs as root and reads only the approval queue.
- Installs and enables the three systemd units (§4).

Run it: `sudo /opt/ares/app/deploy/provision.sh`. It refuses to run outside the
expected layout, so you can't half-apply it.

---

## 4. systemd units

Three long-running services (unit files shipped in `deploy/`):

**`ares.service`** — the daemon.
- `User=ares`, `Group=ares`.
- `EnvironmentFile=/etc/ares/.env` — systemd reads the secrets **as root at
  start** and injects them into the process env, then drops to `ares`. This is
  why the daemon has the secret *values* but cannot read the secret *file*.
- `ExecStart=/opt/ares/venv/bin/python -m instance.main /etc/ares/config.yaml`
- Hardening: `NoNewPrivileges=true`, `ProtectSystem=strict`,
  `ReadWritePaths=/var/lib/ares`, `ProtectHome=tmpfs` with
  `BindPaths=/home/ares-sbx` (hides other homes but lets the runner traverse
  into the sandbox home per §14.2), `PrivateTmp=true`. (The provisioner sets
  these; they enforce the FS contract at the kernel level, not just by
  permissions.)
- `Restart=always`.

**`ares-broker.service`** — the root broker.
- `User=root` (it must execute privileged actions).
- `ExecStart=/usr/bin/python3 /opt/ares/app/broker/aresbrokerd.py /etc/ares/broker.json`
- Stdlib only, so it uses the system `python3`, **not** the ARES venv — it has
  no third-party dependencies and no ARES imports on purpose.
- `Restart=always`. Logs to `/var/log/ares-broker.log`.

**`ares-updater.service`** — the update listener.
- `User=ares-deploy`.
- `EnvironmentFile=/etc/ares/updater.env` (holds `ARES_WEBHOOK_SECRET`).
- `ExecStart=/usr/bin/python3 /opt/ares/app/updater/aresupdater.py /etc/ares/updater.json`
- Stdlib only, system `python3`.
- `Restart=always`.

Enable: `sudo systemctl enable --now ares ares-broker ares-updater`.

---

## 5. Secrets you must set

Put these in `/etc/ares/.env` (mode `0600 root:root`). Never in the repo, never
in `config.yaml` (which only references them via `!secret`).

| Key | Used by | Notes |
|---|---|---|
| `LLM_API_KEY` | daemon | `ollama` for Ollama; real key for a cloud endpoint |
| `DASHBOARD_PASSWORD` | daemon | your dashboard bearer password; make it long |
| `HA_TOKEN` | daemon | Home Assistant long-lived access token |
| `NTFY_TOKEN` | daemon | if your ntfy server needs auth |
| `SIP_PASSWORD` | daemon | ARES's SIP account password |
| `CALDAV_PASSWORD` | daemon | calendar, if used |
| `GITHUB_TOKEN` | daemon (self-edit) | **fine-grained PAT**, single repo, Contents + Pull requests: write. No admin, no merge |

Separately:
- `/etc/ares/updater.env`: `ARES_WEBHOOK_SECRET=<random>` (shared with the
  GitHub webhook config).
- `/etc/ares/broker.json`: the allowlist (spec §16.3). Start it **tight** — only
  the packages/services you actually expect ARES to request. Everything not
  listed is auto-rejected even if you approve it by mistake.

Set `ARES_ENV=prod` in `/etc/ares/.env`. This turns on the tripwires: if the
`.env` is readable by `ares`, or the sandbox user is missing, the daemon refuses
to start rather than silently running everything as one user.

---

## 6. GitHub repo setup (for self-edits)

ARES proposes code changes as PRs you review. To make the gate real:

1. Push the ARES repo to GitHub (`youruser/ares`).
2. **Enable branch protection on `main`:** require a pull request + your review,
   check **Include administrators**, and block force-pushes. This is now
   belt-and-braces rather than the sole defense: since PATCH-3, `open_pr` is
   API-only from the daemon, refuses to target `main`, and only ever creates a
   *new* branch ref — so ARES cannot push to `main` even with a Contents-write
   token. The fine-grained PAT stays scoped to Contents+PRs, **no admin**. You
   are the only merge path. (Strongest option: give the token write only to a
   machine-account **fork** and have ARES PR from the fork.)
3. Create a webhook: Settings → Webhooks → `https://<your-tunnel>/gh`,
   content-type `application/json`, secret = `ARES_WEBHOOK_SECRET`, events =
   just `push`. This drives instant redeploys on merge; the updater's 5-minute
   poll is the backstop if a webhook is missed.
4. Expose the updater's webhook port to GitHub **without opening your VM to the
   internet**: use a Tailscale Funnel or a Cloudflare Tunnel pointed at the
   VM's `webhook_port` (8790). Don't port-forward it raw.

Your operational loop for self-edits: ARES calls `open_pr` → you get a PR →
review the diff → merge (or close) → the webhook fires → the updater verifies,
smoke-imports, swaps the release symlink, and restarts `ares` → ARES is now
running your-approved new code. At no point did ARES run code you didn't merge.

---

## 7. Update flow verification

After first boot, prove the update path works before you rely on it:

1. Merge a trivial no-op PR to `main`.
2. Watch `/var/log/ares-updater.log`: you should see the webhook (or poll)
   detect the new SHA, the smoke import pass, the symlink swap, and the restart.
3. Confirm `/opt/ares/RELEASED_SHA` matches `main`.
4. Break it on purpose once: push a branch whose code fails to import, and
   confirm the updater **aborts the swap** and leaves the running daemon up.
   (Do this on a branch/test flow; never merge broken code to `main`.)

Rollback: the previous release stays in `/opt/ares/releases/`. To roll back,
re-point the `app` symlink at the prior SHA and `systemctl restart ares`.

---

## 8. The dashboard

Once `ares.service` is up, browse to `http://<vm-tailscale-ip>:8788`. Enter the
`DASHBOARD_PASSWORD`. From here you can:

- **Chat** with ARES (this is a WEB-channel conversation; the same session/agent
  as voice and SIP).
- **Memory** — browse the markdown files ARES keeps about you and the home
  (read-only; edit them by SSHing to `/var/lib/ares/memory` as `ares` if you
  want to correct something).
- **Tasks** — see what ARES is tracking, waiting on, and scheduled to do.
- **Approvals** — the important one: pending privilege requests, each showing
  the exact command and ARES's stated reason. **Approve** hands it to the broker
  (which still re-checks the allowlist); **Deny** closes it. This is your
  root-access gate.
- **PRs** — links to ARES's open self-edit pull requests.

Keep the dashboard on the tailnet/LAN only. There's no HTTPS termination in ARES
(spec §out-of-scope) — Tailscale gives you the encrypted transport.

---

## 9. Wiring the external services

- **LLM:** point `llm.base_url` at your Ollama/vLLM/LiteLLM endpoint. If it's on
  the host, use the host's VM-facing IP. Pull the model you configured.
- **Home Assistant:** create a long-lived token → `HA_TOKEN`; set `ws_url` and
  `rest_url`. For face recognition, add HA automations that fire an `ares_event`
  with `{event: face_recognised, who, location, priority}` from your
  Frigate/CompreFace pipeline — ARES consumes the clean fact, never frames.
- **SIP:** register ARES as an extension on your Asterisk; set `SIP_PASSWORD`,
  `server`, `username`. Point your mobile softphone (Linphone/Zoiper) at the
  same Asterisk; set the user's `sip_uri` in config so `place_call` can reach
  you.
- **Push:** stand up ntfy (self-hosted), set `server`/topic and `NTFY_TOKEN`;
  install the ntfy app on your phone and subscribe to the topic.
- **Voice rooms:** if voice runs in-VM, pass the USB audio devices through to
  the microVM and set device names in config. More commonly you'll run room
  satellites elsewhere and feed ARES over the network — either way the config
  maps rooms to devices.

---

## 10. First-boot checklist

- [ ] VM boots, `ares`, `ares-broker`, `ares-updater` all `active (running)`.
- [ ] `sudo -u ares cat /etc/ares/.env` **fails** (permission denied). If it
      succeeds, stop — the security model is broken; fix ownership/mode.
- [ ] Daemon started in `prod` with no tripwire errors in `journalctl -u ares`.
- [ ] Dashboard reachable over Tailscale, password works.
- [ ] Chat round-trips.
- [ ] `run_shell` in a chat runs as `ares-sbx` (`whoami` in a shell command
      returns `ares-sbx`, not `ares`).
- [ ] Ask ARES to request a package install; it appears under Approvals with the
      exact command; approving it makes the broker install it; denying leaves
      the system untouched.
- [ ] A test PR merged to `main` redeploys via the updater.
- [ ] Broker log shows allowlist re-validation on execute.

When every box is ticked, ARES is alive and safely boxed.

---

## 11. Operating notes

- **Back up** `/var/lib/ares` (memory + tasks + queue) — that's ARES's
  accumulated knowledge and state. The code is in git; the state is not.
- **Tighten the broker allowlist over time**, not looser — only add a
  package/service pattern once you've seen ARES legitimately need it.
- **Read PR diffs properly.** The whole self-edit safety story rests on you
  being the reviewer. ARES writing a bad change is expected and harmless; you
  merging one without reading is the only way it becomes live.
- **Rotate `GITHUB_TOKEN` and `DASHBOARD_PASSWORD`** periodically; both live only
  in `/etc/ares/.env`.
- If something feels wrong, `systemctl stop ares` freezes ARES instantly without
  touching the broker/updater or your data.
