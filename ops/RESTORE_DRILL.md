# Restore drill — proving the off-box backup restores (ruling 2026-09-02)

A backup nobody has restored is a hope. Once `/home/agentic/.backup_env`
exists (bucket, keys, passphrase — see `ops/vps/push_backup.sh`), run this
drill ONCE, record the result in SESSION_NOTES, and repeat after any change to
the backup path.

## Status

**BLOCKED as of 2026-09-02:** `/home/agentic/.backup_env` does not exist on the
droplet, so no encrypted tarball has ever been pushed to Spaces and there is
nothing to restore from. The on-box nightly tarballs
(`/var/backups/agentic/data-YYYY-MM-DD.tar.gz`) do exist; the drill script
accepts one of those with `--local` so the restore+verify half can be exercised
today, but that proves the tarball, not the off-box path.

## The drill

```
cd /home/agentic/Agentic
sudo install -m 755 ops/vps/restore_drill.sh /usr/local/bin/agentic-restore-drill
agentic-restore-drill                      # newest .gpg from Spaces
agentic-restore-drill --local /var/backups/agentic/data-2026-09-02.tar.gz
```

The script never touches `data/`. It:

1. Fetches the newest `agentic/data-*.tar.gz.gpg` from `spaces:$SPACES_BUCKET`
   (or takes the `--local` tarball), decrypts it with `BACKUP_PASSPHRASE`, and
   extracts it into a fresh scratch directory `/tmp/agentic-restore-<stamp>/`.
2. Runs `ops/vps/restore_verify.py` against the scratch `data/` and the live
   `data/`: both audit logs are parsed record-by-record with the production
   parser (a corrupt or truncated line fails the drill loudly), then compared —
   record counts by kind, the last record's timestamp, open positions per
   strategy replayed from each log, and the session-state kill switch / halt
   flags.
3. Prints PASS when the scratch copy parses completely and its content is a
   prefix of the live log (the live log may only have grown since the backup);
   FAIL otherwise, with the first divergence.

Expected on a healthy day: scratch is N records behind live (N = everything
written since last night's 01:07 UTC tarball), identical up to that point.

## Recording the result

Append to SESSION_NOTES under a dated heading: the tarball name, its age, record
counts (scratch vs live), open positions matched, and PASS/FAIL. A FAIL is a
ticket, not a footnote.
