# drivecon

Catalogs old drives into one database, then copies each unique document once to an NTFS
destination drive, keeping original names and folders (merged across drives). Sources are
never modified. Browse the result directly from Windows or Linux: no links, no hash names.

## One-time setup

1. Install Docker (Engine, or Docker Desktop with the WSL2 backend) and, on the host:
   `smartmontools`, `ntfs-3g` (or the kernel `ntfs3` driver), `exfatprogs`/`exfat-fuse`,
   `cifs-utils`, plus `dislocker`, `cryptsetup`, `mdadm`, `lvm2`, `hfsprogs` for odd drives.
2. Mount the NTFS destination drive, e.g. at `/mnt/dest`, owned by your user.
3. Create the folders and set the environment (put it in a `.env` file next to `compose.yaml`):

   ```
   sudo mkdir -p /srv/sources /srv/intake
   DRIVECON_UID=$(id -u)  DRIVECON_GID=$(id -g)
   DRIVECON_DEST_DIR=/mnt/dest
   DRIVECON_CATALOG_MODE=native      # use "wsl" when the destination is /mnt/<letter> in WSL2
   ```
4. Build once: `docker compose build drivecon`.

## For each drive

```
sudo scripts/intake.sh /dev/sdX Dell-2009-C      # SMART check, read-only mount, intake sheet
docker compose run --rm drivecon run Dell-2009-C # scan + hash + copy; asks about new extensions
docker compose run --rm drivecon report sources  # which drives are safe to retire
```

`intake.sh` writes `_intake/<label>.txt` on the destination: print it and tape it to the drive.
Run long jobs in `tmux`. Every phase is resumable: if a run is interrupted, run it again.

Unknown extensions are held, and the rest of the work continues. Answer the prompts as they
appear, or later with `drivecon decide`; answers are written to `config/file-types.toml`.

Useful commands (all `docker compose run --rm drivecon ...`):

| command | what it does |
|---|---|
| `dry-run <label>` | scan and report what would be copied, writing nothing |
| `decide` | answer queued unknown-extension prompts |
| `report sources / types / errors / conflicts / renamed` | see the state of things |
| `where <path or hash>` | every original location of a file |
| `retry-errors <label>` | re-attempt files that failed to read |
| `verify` | re-hash every copied file against the catalog |

## The secondary drive

A second, independent NTFS drive, kept in step with `sync-mirror`:

```
sudo blkid -s UUID -o value /dev/sdY1                 # the NTFS volume serial
docker compose run --rm drivecon-mirror mirror register Sec-A --serial <serial>
docker compose run --rm drivecon-mirror sync-mirror   # after each drive
docker compose run --rm drivecon-mirror verify --mirror
```

Set `DRIVECON_MIRROR_DIR` to where the secondary is mounted. `sync-mirror` never deletes;
extra files are listed in `_reports/mirror-extra.csv` and removed only with `--prune`.
If the primary fails, the secondary already holds a recent catalog: mount it as the new
primary and register a replacement as the new secondary.

## Development

Everything runs in the dev container:

```
docker compose run --rm dev pytest -m "unit or component"
docker compose run --rm dev sh -c "ruff check . && mypy"
docker compose run --rm dev-root pytest -m system      # loop devices; privileged
```

Image-mock tests need root and loop devices and skip with a reason otherwise. The container
smoke test (`pytest -m container`) builds the runtime image and needs Docker on the host.
