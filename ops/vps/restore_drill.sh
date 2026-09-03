#!/usr/bin/env bash
# Restore drill (human ruling 2026-09-02) — see ops/RESTORE_DRILL.md.
# Restores the newest off-box backup (or a --local tarball) into a scratch
# directory and verifies it against the live data/ with restore_verify.py.
# NEVER writes to /home/agentic/Agentic/data.
set -euo pipefail

REPO=/home/agentic/Agentic
ENV_FILE=/home/agentic/.backup_env
LOCAL=""
if [[ "${1:-}" == "--local" ]]; then
    LOCAL="${2:?--local needs a tarball path}"
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
scratch=$(mktemp -d "/tmp/agentic-restore-${stamp}-XXXX")
echo "scratch: $scratch"

if [[ -n "$LOCAL" ]]; then
    echo "source: local tarball $LOCAL (proves the tarball, NOT the off-box path)"
    tar -xzf "$LOCAL" -C "$scratch"
else
    if [[ ! -f "$ENV_FILE" ]]; then
        echo "BLOCKED: $ENV_FILE does not exist; no off-box backup has ever been pushed." >&2
        echo "Create it per ops/vps/push_backup.sh, let one nightly push run, then re-run." >&2
        exit 3
    fi
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    newest=$(rclone lsf "spaces:${SPACES_BUCKET}/agentic/" --include 'data-*.tar.gz.gpg' | sort | tail -1)
    if [[ -z "$newest" ]]; then
        echo "FAIL: no data-*.tar.gz.gpg objects in spaces:${SPACES_BUCKET}/agentic/" >&2
        exit 1
    fi
    echo "source: spaces:${SPACES_BUCKET}/agentic/${newest}"
    rclone copyto "spaces:${SPACES_BUCKET}/agentic/${newest}" "$scratch/backup.tar.gz.gpg"
    gpg --batch --yes --quiet --passphrase "$BACKUP_PASSPHRASE" \
        --output "$scratch/backup.tar.gz" --decrypt "$scratch/backup.tar.gz.gpg"
    tar -xzf "$scratch/backup.tar.gz" -C "$scratch"
    rm -f "$scratch/backup.tar.gz" "$scratch/backup.tar.gz.gpg"
fi

if [[ ! -f "$scratch/data/audit.jsonl" ]]; then
    echo "FAIL: restored tree has no data/audit.jsonl" >&2
    exit 1
fi

cd "$REPO"
.venv/bin/python ops/vps/restore_verify.py "$scratch/data" "$REPO/data"
status=$?
echo "scratch left in place for inspection: $scratch (rm -rf it when done)"
exit $status
