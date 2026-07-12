#!/usr/bin/env bash
#
# ARES VM provisioning — creates the user/permission separation the whole
# security model depends on (spec §14). Idempotent: safe to re-run. Run as
# root INSIDE the target VM:  sudo /opt/ares/app/deploy/provision.sh
#
# It creates the three no-login service users, lays out the filesystem with the
# exact owners/modes of §14.2, installs the two narrow sudoers entries, and
# installs+reloads the systemd units. It does NOT create secrets or configs —
# those are placed by the operator (see DEPLOYMENT.md).
set -euo pipefail

APP_DIR=/opt/ares/app
ETC_DIR=/etc/ares
STATE_DIR=/var/lib/ares
SBX_HOME=/home/ares-sbx
SUDOERS_FILE=/etc/sudoers.d/ares
SYSTEMD_DIR=/etc/systemd/system

log() { printf '[provision] %s\n' "$*"; }
die() { printf '[provision] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || die "must run as root (try: sudo $0)"
[[ -d "${APP_DIR}" ]] || die "${APP_DIR} not found — deploy the app tree before provisioning (DEPLOYMENT.md §2)"

# 1. No-login service users (§14.1). --system, nologin shell, no home except sbx.
ensure_user() {
    local user="$1" home="$2"
    if id -u "${user}" >/dev/null 2>&1; then
        log "user ${user} already exists"
        return
    fi
    if [[ -n "${home}" ]]; then
        useradd --system --create-home --home-dir "${home}" \
            --shell /usr/sbin/nologin "${user}"
    else
        useradd --system --no-create-home --shell /usr/sbin/nologin "${user}"
    fi
    log "created user ${user}"
}
ensure_user ares ""
ensure_user ares-sbx "${SBX_HOME}"
ensure_user ares-deploy ""

# 2. Filesystem contract (§14.2).
# /opt/ares owned by ares-deploy:ares; the live app tree is 0750 (RO to ares).
chown -R ares-deploy:ares /opt/ares
chmod 0750 /opt/ares
[[ -e "${APP_DIR}" ]] && chmod -R g-w "${APP_DIR}"

# /etc/ares: config readable by ares; secrets/broker/updater 0600 root:root.
install -d -o root -g ares -m 0750 "${ETC_DIR}"
[[ -f "${ETC_DIR}/config.yaml" ]] && chown root:ares "${ETC_DIR}/config.yaml" && chmod 0640 "${ETC_DIR}/config.yaml"
for secret in .env updater.env broker.json updater.json; do
    if [[ -f "${ETC_DIR}/${secret}" ]]; then
        chown root:root "${ETC_DIR}/${secret}"
        chmod 0600 "${ETC_DIR}/${secret}"
    fi
done

# /var/lib/ares: all mutable state, 0700 ares:ares.
install -d -o ares -g ares -m 0700 "${STATE_DIR}"
install -d -o ares -g ares -m 0700 "${STATE_DIR}/memory"

# sandbox scratch clone, owned by ares-sbx.
install -d -o ares-sbx -g ares-sbx -m 0700 "${SBX_HOME}/scratch"

# 3. The two narrow sudoers entries (§14.1). Nothing else gets sudo.
#  - ares may drop to ares-sbx to run sandboxed shells (§15 uses /bin/bash -lc).
#  - ares-deploy may restart the ares unit (the updater's one privileged action).
umask 077
cat > "${SUDOERS_FILE}.tmp" <<'EOF'
# Managed by ARES deploy/provision.sh — do not edit by hand.
ares ALL=(ares-sbx) NOPASSWD: /bin/bash
ares-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart ares
EOF
chmod 0440 "${SUDOERS_FILE}.tmp"
if visudo -c -f "${SUDOERS_FILE}.tmp" >/dev/null; then
    mv "${SUDOERS_FILE}.tmp" "${SUDOERS_FILE}"
    log "installed sudoers ${SUDOERS_FILE}"
else
    rm -f "${SUDOERS_FILE}.tmp"
    die "sudoers validation failed; not installing"
fi

# 4. systemd units.
for unit in ares.service ares-broker.service ares-updater.service; do
    install -o root -g root -m 0644 "${APP_DIR}/deploy/${unit}" "${SYSTEMD_DIR}/${unit}"
    log "installed ${unit}"
done
systemctl daemon-reload

log "done. Enable with: systemctl enable --now ares ares-broker ares-updater"
