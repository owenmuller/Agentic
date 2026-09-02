#!/usr/bin/env bash
# Off-box backup push (human ruling 2026-09-02, operational hardening).
#
# The audit log is the system of record; until this runs configured, its only
# copies live on the one box that could lose them. The nightly backup unit
# already writes /var/backups/agentic/data-YYYY-MM-DD.tar.gz (14-day on-box
# retention); this script encrypts the newest tarball CLIENT-SIDE (gpg
# symmetric, AES256) and pushes it to a DigitalOcean Spaces bucket via rclone,
# then prunes remote copies older than 90 days.
#
# NOT CONFIGURED = NOT AN ERROR: without /home/agentic/.backup_env the script
# logs one line and exits 0, so the nightly unit stays green until the human
# creates the bucket and keys. Configure by writing /home/agentic/.backup_env
# (chmod 600), with:
#
#   SPACES_BUCKET=<bucket name>
#   BACKUP_PASSPHRASE=<long random string; losing it loses the backups>
#   RCLONE_CONFIG_SPACES_TYPE=s3
#   RCLONE_CONFIG_SPACES_PROVIDER=DigitalOcean
#   RCLONE_CONFIG_SPACES_ACCESS_KEY_ID=<Spaces access key>
#   RCLONE_CONFIG_SPACES_SECRET_ACCESS_KEY=<Spaces secret>
#   RCLONE_CONFIG_SPACES_ENDPOINT=nyc3.digitaloceanspaces.com
#
# and installing rclone (apt install rclone). Restore drill:
#   rclone copyto spaces:$SPACES_BUCKET/agentic/<name>.gpg /tmp/x.tar.gz.gpg
#   gpg -d /tmp/x.tar.gz.gpg > /tmp/x.tar.gz   (asks for BACKUP_PASSPHRASE)
set -euo pipefail

ENV_FILE=/home/agentic/.backup_env
if [[ ! -f "$ENV_FILE" ]]; then
    logger -t agentic-backup "off-box push not configured (no $ENV_FILE); on-box tarball only"
    exit 0
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

latest=$(ls -1t /var/backups/agentic/data-*.tar.gz 2>/dev/null | head -1)
if [[ -z "${latest:-}" ]]; then
    logger -t agentic-backup "no on-box tarball found to push"
    exit 1
fi

tmp=$(mktemp /tmp/agentic-backup.XXXXXX.gpg)
trap 'rm -f "$tmp"' EXIT
gpg --batch --yes --symmetric --cipher-algo AES256 \
    --passphrase "$BACKUP_PASSPHRASE" --output "$tmp" "$latest"

rclone copyto "$tmp" "spaces:${SPACES_BUCKET}/agentic/$(basename "$latest").gpg"
rclone delete --min-age 90d "spaces:${SPACES_BUCKET}/agentic/" || true
logger -t agentic-backup "pushed $(basename "$latest").gpg to spaces:${SPACES_BUCKET}/agentic/"
