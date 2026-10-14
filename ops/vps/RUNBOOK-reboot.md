# Droplet reboot runbook — Saturday 2026-10-17

For: the operator with root on the droplet (`ssh agentic@137.184.59.200`, then `sudo`).
Time: about 30 minutes, plus up to an hour if you also rotate keys (§3).
Why: 45 package updates are pending (including `libc6` and kernel `6.8.0-147`). The running kernel is `6.8.0-124`, unchanged for 55 days, and `/var/run/reboot-required` is set.
Saturday is safe: markets are closed, no trading timer fires, and the next session starts Monday 09:15 ET.

## 1. Pre-checks (as `agentic`, before any change)

1. Baseline: `SKIP_CARDS=1 bash ~/Agentic/ops/vps/post_reboot_check.sh`
   Expect exactly two FAILs, "reboot still required" and "uptime …". Everything else should be PASS or INFO. Note anything different.
2. Nothing is running: `systemctl is-active agentic-paper agentic-lab-ai-trader agentic-lab-ladder agentic-momentum 2>/dev/null` — all `inactive`/`unknown`.
   `systemctl --user list-units 'research-*' --no-legend` — empty.
3. Snapshot first (your rollback): DigitalOcean console → Droplets → this droplet → Backups & Snapshots → **Take live snapshot**. Wait until it shows as complete.

## 2. Fixes while you have root (each independent; 2a recommended, 2b recommended, 2c optional)

a. **Backup unit fails every night (exit 203)**: its second step `/usr/local/bin/agentic-push-backup` was never installed. Install it (unconfigured, it logs one line and exits 0):

```
sudo install -m 755 /home/agentic/Agentic/ops/vps/push_backup.sh /usr/local/bin/agentic-push-backup
sudo systemctl start agentic-backup.service && systemctl status agentic-backup --no-pager | head -5
```

Expect `status=0/SUCCESS`; this also makes a fresh on-box tarball before the reboot. To push backups off-box, do §6 as well.

b. **No swap on a 961 MB box** (a monthly momentum prep peaks near 500 MB). Add 2 GB:

```
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf && sudo sysctl --system >/dev/null
```

c. **A one-shot timer left over from August**: `sudo systemctl disable --now agentic-rh-refresh.timer`

## 3. Key rotation (best done now, before the reboot, so every service starts on the new keys)

`.env` was last modified 2026-10-12 21:00 UTC, so nothing has been rotated since then. Only the Discord webhooks are provably newer: created 10-12 20:22 and 20:25 UTC. Rotate in this order (highest blast radius first):

1. `ANTHROPIC_API_KEY` (real spend): console.anthropic.com → Settings → API Keys → create a new key, put it in `.env`, then disable the old one.
2. `ALERT_SMTP_PASSWORD` (sends mail as you): Google Account → Security → App passwords → revoke the old, create a new one, put it in `.env`.
3. `ALPACA_API_KEY/_SECRET`, `ALPACA_LAB_LADDER_*`, `ALPACA_LAB_AI_TRADER_*` (can trade the paper accounts): app.alpaca.markets → switch to each paper account → API Keys → Regenerate (the old key dies at once), put the pair in `.env`.
4. `DISCORD_WEBHOOK_URGENT`, `DISCORD_WEBHOOK_REPORTS` (fake cards): skip if you created them after the exposure. Otherwise: channel → Edit Channel → Integrations → Webhooks → delete it, create a new one, copy its URL into `.env`.
5. `X_BEARER_TOKEN`: developer.x.com → your app → Keys and tokens → Bearer Token → Regenerate.
6. `QUIVER_API_KEY`: quiverquant.com → Account → API → regenerate the token (if not offered, ask their support).
7. `FINNHUB_API_KEY`: finnhub.io → Dashboard → regenerate the API key.
8. `NTFY_TOPIC` (the topic name is the secret): pick a new random name (`openssl rand -hex 12`), put it in `.env`, resubscribe the phone app to it.

`ALERT_TO`, `ALERT_SMTP_USER` and `PAPER_MODE` are not secrets.
After each edit of `~/Agentic/.env`, run:

```
~/Agentic/.venv/bin/python ~/Agentic/ops/vps/verify_keys.py
```

Every line should read PASS; SMTP reads UNVERIFIED from the droplet.

## 4. Apply updates

```
sudo apt update
sudo apt full-upgrade -y
sudo apt autoremove -y
cat /var/run/reboot-required.pkgs
```

If apt asks about a changed config file, keep the installed version (the default answer).

## 5. Reboot and post-checks

```
sudo reboot
```

Wait 1–2 minutes and ssh back in as `agentic`, then:

```
bash ~/Agentic/ops/vps/post_reboot_check.sh
```

Expect `ALL PASS` and the new kernel on the first line. This run also sends one test card to #urgent and one to #reports; check that both arrived.

It confirms:
- no reboot is pending;
- every `agentic-*` timer is enabled with a next run;
- the persistent checks (`check-deploy-…`, `check-momentum-step2` on Nov 6) are still enabled with `Persistent=yes`;
- lingering is on;
- no transient timer exists;
- every key verifies;
- health runs.

If something fails:

| Failure | Fix |
|---|---|
| an `agentic-*` timer not enabled | `sudo systemctl enable --now agentic-NAME.timer` |
| a `check-*` timer missing or disabled | `systemctl --user daemon-reload && systemctl --user enable --now check-NAME.timer` |
| lingering OFF | `sudo loginctl enable-linger agentic` |
| a key FAILs | fix that line in `.env`, rerun `verify_keys.py` |
| a test card did not arrive | `python -m orchestrator health` shows the alert transport error; the webhook URL in `.env` |
| droplet does not come back | DO console → Recovery console; worst case restore the §1 snapshot |

Then tell Claude Code: "post-reboot check: ALL PASS" (or paste the FAIL lines) so it is recorded in SESSION_NOTES.

## 6. Off-box backups (DigitalOcean Spaces) — today nothing leaves the droplet

1. DO console → **Spaces Object Storage** → **Create Bucket**:
   - region **NYC3** (the droplet is in NYC1; NYC3 is the nearest Spaces region and what the push script expects);
   - name e.g. `agentic-backups-<random>`;
   - file listing **Restricted**.
2. DO console → **API** → **Spaces Keys** → **Generate New Key**, name `agentic-droplet-backup`. If "Limited access" is offered, grant read/write/delete on that bucket only. Copy the access key and the secret; the secret is shown once.
3. On the droplet, as `agentic`: create `/home/agentic/.backup_env` with `chmod 600`. Not `.env`, not git. Contents:

   ```
   SPACES_BUCKET=<bucket name>
   BACKUP_PASSPHRASE=<output of: openssl rand -base64 48>
   RCLONE_CONFIG_SPACES_TYPE=s3
   RCLONE_CONFIG_SPACES_PROVIDER=DigitalOcean
   RCLONE_CONFIG_SPACES_ACCESS_KEY_ID=<access key>
   RCLONE_CONFIG_SPACES_SECRET_ACCESS_KEY=<secret>
   RCLONE_CONFIG_SPACES_ENDPOINT=nyc3.digitaloceanspaces.com
   ```

   **Put a copy of `BACKUP_PASSPHRASE` in your password manager. Losing it loses every backup.**
4. `sudo apt install -y rclone` (gpg is already installed).
5. If not done in §2a, install the push script. Then:

   ```
   sudo systemctl start agentic-backup.service
   journalctl -t agentic-backup -n 3 --no-pager
   ```

   Expect `pushed data-YYYY-MM-DD.tar.gz.gpg to spaces:…`.
6. Prove a restore: `bash ~/Agentic/ops/vps/restore_drill.sh`. It restores into a scratch directory and never touches `data/`.
