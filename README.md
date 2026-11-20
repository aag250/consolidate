# drivecon

drivecon consolidates a pile of old drives, SD cards and USB sticks into one backup.

It catalogs every drive in a single database, then copies each **unique** document once to an
NTFS destination drive, keeping its original name and folder. Folder trees from different
drives are merged into one. The result can be browsed directly from Windows Explorer or a
Linux file manager: no links, no hash-named files. **Source drives are never modified.**

- [How it works](#how-it-works)
- [Setup](#setup)
- [Processing a drive](#processing-a-drive)
- [Command reference](#command-reference)
- [The destination drive](#the-destination-drive)
- [Configuration](#configuration)
- [The secondary (mirror) drive](#the-secondary-mirror-drive)
- [Windows and WSL2](#windows-and-wsl2)
- [Safety and recovery](#safety-and-recovery)
- [Development and testing](#development-and-testing)

## How it works

Each drive goes through five phases. Every phase is idempotent and resumable, so drives can
be processed one at a time, weeks apart, and an interrupted run is simply run again.

| Phase | What happens |
|---|---|
| **Intake** | The host identifies the drive (model, serial, capacity, filesystem, UUID), reads SMART health, mounts it read-only, and writes a printable intake sheet. A drive already registered by UUID is recognized on rescan. |
| **Scan** | Walks the filesystem applying rules in order: path exceptions, path exclusions, junk-file rules, file types. Included files are recorded in the catalog; excluded trees are stored as per-directory counts. Extension-less files are sniffed with libmagic. |
| **Hash** | Every included file is fully hashed with BLAKE3. This runs in a worker thread alongside the scan. |
| **Place and copy** | A hash already in the catalog just gets the new location recorded. A new hash gets a destination path, is copied to a temporary name, fsynced, re-hashed, and renamed into place. |
| **Report and verify** | Per-drive results are appended to its intake sheet, with a **safe to retire** verdict. `verify` re-hashes every copied file against the catalog. |

### What gets kept

Only the file types listed in `config/file-types.toml` (office documents, PDFs, photos, video,
audio, email archives, source code, and so on). Applications, OS files, application data and
settings are excluded by `config/paths.toml`. An extension on neither list is **held**: the
tool asks you about it (include, exclude, or decide later) while the rest of the work
continues, and writes your answer back to `config/file-types.toml` so it applies to every
later drive.

Exact duplicates are dropped, but every place each file was found stays in the catalog
(`drivecon where`).

### Where files land

One NTFS volume with this layout:

```
Files/                  merged document tree, original names
Files/Photos/YYYY/MM/   camera-dump images, by EXIF date (else oldest modified time)
_catalog/               catalog.sqlite + dated backups
_intake/                one .txt sheet (and .json) per drive
_reports/               conflicts, renamed paths, read errors
```

**Merging trees.** Each source path is stripped to its meaningful part: drive letters,
`Users/<name>/Documents/`, `Users/<name>/`, `Documents and Settings/<name>/My Documents/`,
`home/<user>/` and backup wrappers like `Backup 2012/` are removed (extra prefixes per drive
can be given at intake with `--strip-prefix`). So
`Users/joe/Documents/finance/taxes/2011/w2.pdf` on one drive and
`Backup 2012/finance/taxes/2011/w2.pdf` on another both land at
`Files/finance/taxes/2011/w2.pdf`.

**Best path.** When one file exists at several paths, the best-scoring path wins, in order:
a path seen on more than one drive; deeper named folders over `Desktop/`, `Downloads/` or a
drive root; no `backup`, `copy`, `old`, `(1)` in the name; the oldest modified time. Later
drives never move a file that is already placed.

**Name conflicts.** Two *different* files that map to the same path both stay. The later one
gets its source and date added: `budget (Dell-2009-C, 2011-03-04).xlsx`. Comparison is
case-insensitive, as NTFS is. Conflicts are listed in `_reports/conflicts.csv`.

**NTFS-safe names.** Characters Windows forbids (`\ : * ? " < > |` and control characters)
become `_`, trailing dots and spaces are trimmed, reserved names (`CON`, `NUL`, `COM1`...)
get a leading `_`, and full paths are kept under 240 characters by shortening the deepest
folder names first. Every changed name is logged in the catalog and `_reports/renamed.csv`.

**Photos.** Images in a camera dump (`DCIM/`, folders like `100CANON`, `Camera Roll/`, or the
root of an SD card) go to `Files/Photos/YYYY/MM/`. Images in named folders stay where they
were.

### Retire-safe

`report sources` tells you which drives are safe to retire. A drive is safe when it was fully
scanned, nothing is held for an unknown extension, no file failed to read, every included file
is copied (or recorded as a duplicate of a copied file), and every such blob has been verified
in the destination. Otherwise the reasons are listed.

## Setup

Requirements on the machine that does the work (a Linux box, or a Windows PC running WSL2):

- Docker Engine, Podman, or Docker Desktop with the WSL2 backend, including `docker compose`.
- Host tools for mounting and SMART: `smartmontools`, `ntfs-3g` (or the kernel `ntfs3`
  driver), `exfatprogs` / `exfat-fuse`, `cifs-utils`, and for unusual drives `dislocker`,
  `cryptsetup`, `mdadm`, `lvm2`, `hfsprogs`.

The tool itself runs as one **unprivileged** container. The host does all mounting and hands
the container read-only source folders, so the container cannot alter a source. Sources are
read-only twice: mounted `ro` on the host and bind-mounted `:ro`.

One-time setup:

```bash
git clone git@github.com:aag250/consolidate.git && cd consolidate

# Mount the NTFS destination drive, e.g. at /mnt/dest, owned by your user.
sudo mkdir -p /srv/sources /srv/intake

# Settings live in a .env file next to compose.yaml:
cat > .env <<EOF
DRIVECON_UID=$(id -u)
DRIVECON_GID=$(id -g)
DRIVECON_DEST_DIR=/mnt/dest
DRIVECON_CATALOG_MODE=native     # "wsl" when the destination is /mnt/<letter> under WSL2
EOF

docker compose build drivecon
```

| Variable | Default | Meaning |
|---|---|---|
| `DRIVECON_UID` / `DRIVECON_GID` | 1000 | Owner of the mounted destination; the container runs as this user |
| `DRIVECON_DEST_DIR` | `/mnt/dest` | Host path of the destination drive |
| `DRIVECON_SRV` | `/srv` | Holds `sources/<label>` mount points and `intake/<label>.json` |
| `DRIVECON_CATALOG_MODE` | `native` | `native` or `wsl` (see [Windows and WSL2](#windows-and-wsl2)) |
| `DRIVECON_MIRROR_DIR` | `/mnt/mirror` | Host path of the secondary drive |

## Processing a drive

```bash
# 1. Attach the drive. On the host, as root:
sudo scripts/intake.sh /dev/sdX Dell-2009-C

# 2. Scan, hash and copy. Run long jobs in tmux.
docker compose run --rm drivecon run Dell-2009-C

# 3. See where things stand.
docker compose run --rm drivecon report sources
```

**Step 1: `intake.sh`** runs on the host because it needs root and the raw device. It collects
`lsblk`, `blkid` and `smartctl` data, mounts the source read-only under
`/srv/sources/<label>` (refusing to continue if it is not read-only), saves the JSON to
`/srv/intake/<label>.json`, and runs the container's `intake` command. That registers the
drive and writes `_intake/<label>.txt` to the destination. **Print the sheet and tape it to
the drive.**

If SMART reports FAILED, or any reallocated, pending or offline-uncorrectable sector count is
above zero, you are asked whether to continue; continuing reads normally and logs per-file
read errors. SD cards, most USB sticks, network shares and Windows-mounted drives under WSL2
are recorded as "SMART not available" (not a problem). Labels may use letters, digits, `.`,
`_` and `-`.

Options: `--partition /dev/sdX1` to pick a partition, `--smart-json FILE` to supply SMART data
collected elsewhere.

**Network shares** have no block device. Mount the share read-only on the host under
`/srv/sources/<label>` (`mount -t cifs //pc/share /srv/sources/<label> -o ro,...`), then
register it directly with
`docker compose run --rm drivecon intake --label <label> --source-dir /sources/<label>`.
SMART is recorded as not available.

**Step 2: `run`** scans, hashes and copies. When the scan meets an extension that is on
neither list, it prompts:

```
Unknown extension '.xyz': 214 files, 1.3 GB so far
    e.g. Users/joe/Documents/old/thing.xyz
    e.g. ...
[i]nclude, [e]xclude, [d]ecide later:
```

Scanning and hashing carry on behind the prompt. Held files are processed once you answer.
With no terminal (unattended runs), prompts are queued instead; answer them later with
`decide`, and look at them with `report types`.

**Step 3: `report sources`** shows each drive's files seen, included, new unique files and
bytes, duplicates, and whether it is safe to retire, plus how far the secondary is behind.
After each run the same results are appended to the drive's intake sheet.

A cautious first drive: run `dry-run <label>` first. It scans into a throwaway catalog and
reports what would be copied, writing nothing to `Files/` or the real catalog.

## Command reference

All commands run as `docker compose run --rm drivecon <command>` (or `drivecon-mirror` when
the secondary is needed). Paths inside the container are `/sources`, `/dest`, `/config`,
`/intake` and `/mirror`; the compose file wires them up.

| Command | What it does |
|---|---|
| `intake --label L --device-info J` | Register a mounted source, check SMART, write `_intake/L.txt`. Called by `intake.sh`; use `--source-dir` instead of `--device-info` for a network share. `--strip-prefix P` (repeatable) strips an extra folder prefix for this drive. `--yes` continues past SMART warnings. |
| `scan L` | Walk and classify; safe to re-run. |
| `hash L` | Hash included files not yet hashed. |
| `copy L` | Place and copy new files to `Files/`, verifying each. |
| `run L` | Scan, hash and copy in sequence, with unknown-extension prompts. |
| `dry-run L` | Scan and report what would be copied; writes nothing. |
| `decide` | Answer queued unknown-extension prompts. |
| `retry-errors L` | Re-attempt files that failed to read (then `run L`). |
| `verify` | Re-hash every copied file against the catalog; reports missing, corrupt and unexpected files. |
| `where X` | Every original location of a file, by hash or by (part of) a path. |
| `report sources` | Per-drive results, retire-safe flag, secondary lag. |
| `report types` | Unknown and pending extensions by count and size. |
| `report errors` | Files that failed to read. |
| `report conflicts` / `report renamed` | Same-path conflicts and NTFS name changes. |
| `mirror register NAME --serial S` | Register the secondary drive. |
| `sync-mirror [--prune] [--yes]` | Bring the secondary up to date. |
| `verify --mirror` | Re-hash every file on the secondary against the catalog. |

Exit codes: `0` success; `1` problems found (`verify`, sync errors); `2` bad input or a
secondary that is not plugged in or not registered; `3` you declined to continue past a SMART
warning; `4` destination or secondary full; `130` interrupted.

### Examples

```bash
# Where did this file come from, and where is it now?
docker compose run --rm drivecon where finance/taxes/2011/w2.pdf
# Dell-2009-C: Users/joe/Documents/finance/taxes/2011/w2.pdf  [copied]    -> Files/finance/taxes/2011/w2.pdf
# USB-backup:  Backup 2012/finance/taxes/2011/w2.pdf          [duplicate] -> Files/finance/taxes/2011/w2.pdf

# Which extensions are waiting for a decision, biggest first?
docker compose run --rm drivecon report types

# A drive had read errors: look, retry, re-run.
docker compose run --rm drivecon report errors
docker compose run --rm drivecon retry-errors Dell-2009-C
docker compose run --rm drivecon run Dell-2009-C
```

## The destination drive

The destination is plain NTFS, readable on its own on Windows or Linux. The catalog lives on
it (`_catalog/catalog.sqlite`), so the drive can move to another machine and work resumes.

- **Native Linux:** SQLite runs directly on the drive in rollback-journal mode (never WAL,
  which is unreliable on NTFS) with a single writer. A dated copy is written to
  `_catalog/backups/` after each drive.
- **WSL2:** see below.

Everything else on it is plain files: `Files/` is the browsable tree; `_intake/`, `_reports/`
and `_catalog/` are the records.

## Configuration

Two files in `config/`, read at start-up:

- **`file-types.toml`**: categories of extensions with an `include` or `exclude` decision
  (and regex `patterns` for year-numbered extensions like `tax2011`). Your answers to
  unknown-extension prompts are appended as `[[decided]]` tables with the date and an example
  path. Comments and order are preserved when the tool writes to it.
- **`paths.toml`**: directory exclusions (Windows system, per-user app data, Linux system,
  caches, `node_modules`...), path exceptions (Outlook `.pst` files under `AppData` are
  rescued), junk-file rules (`Thumbs.db`, `desktop.ini`, `~$*` lock files, zero-byte files,
  an optional image size floor), the folder prefixes stripped when merging, and the
  camera-dump rules.

Exclusions have an `os` field (`windows`, `linux` or `any`), chosen per source from its
filesystem type, so Linux system rules never hit a Windows drive's top-level `Dev` or `Run`
folder. Rules apply in this order, first match wins: path exceptions, path exclusions,
junk-file rules, file types.

Test fixtures are generated from these files, so adding an extension or a rule without a
matching fixture fails the test suite.

## The secondary (mirror) drive

Two independent NTFS drives, rather than a software mirror (which would tie you to one OS).
Each is readable on its own on any machine, and the secondary can be kept unplugged or
off-site between syncs, which also protects against deletion, ransomware, theft and fire.

```bash
sudo blkid -s UUID -o value /dev/sdY1                  # the secondary's NTFS volume serial
docker compose run --rm drivecon-mirror mirror register Sec-A --serial <serial>   # once
docker compose run --rm drivecon-mirror sync-mirror    # after each drive
docker compose run --rm drivecon-mirror verify --mirror
```

- `sync-mirror` copies everything new: files, intake sheets, reports and a fresh catalog copy.
  Each file goes to a temporary name, is fsynced, re-hashed against the catalog and renamed;
  its sync is recorded per drive. It is resumable at any point.
- It **never deletes**. Files that should no longer be there are listed in
  `_reports/mirror-extra.csv` and removed only with `sync-mirror --prune`, after confirmation.
- A drive that is not registered, or is not plugged in, is refused (exit code 2).
- `report sources` shows how far the secondary is behind, and each run reminds you to sync.
- If the primary fails, the secondary already holds a recent catalog: mount it as the new
  primary, register a replacement as the new secondary, and sync.

A mirror protects against one disk dying, not against corruption you copy into it. Once
consolidation is done, make one further copy offsite or on a cold external drive.

## Windows and WSL2

Both setups are first-class: everything on native Linux, or the destination drive mounted on a
Windows PC with the tool running in WSL2 on that PC. The same image runs in both.

- Windows mounts most drives itself and exposes them at `/mnt/<letter>` (the 9P/drvfs
  protocol). Remount a source read-only in WSL: `mount -t drvfs E: /srv/sources/<label> -o ro`.
  That covers NTFS, FAT32 and exFAT disks, USB sticks and SD cards. BitLocker drives: unlock
  in Windows Explorer first.
- ext4 and other Linux-format disks: `wsl --mount \\.\PHYSICALDRIVEn --partition N` from an
  admin PowerShell (whole disks only; Windows must not be using the disk). This does **not**
  work for USB sticks and SD card readers; use native Linux or a live USB for those.
- SMART cannot be read for drives Windows has mounted. Run `smartctl` from the Windows build of
  smartmontools and pass its JSON with `intake.sh --smart-json`, or accept "not available".
- Set `DRIVECON_CATALOG_MODE=wsl`. SQLite file locking is unreliable on drvfs, so the tool
  keeps a working copy of the catalog on WSL's own disk (the `catalog-work` volume) and copies
  it to `_catalog/` on the destination after every committed batch and at exit. On start it
  uses whichever copy is newer, so the copy on the drive is always current enough to move the
  drive to another machine.

## Safety and recovery

- **Sources are never written.** The tool refuses to start if a source mount is writable.
- **A crash never leaves a half-written file under its final name.** Copies go to a temp name
  (`.dc-<hash>.tmp`), are fsynced, re-hashed and renamed. Leftover temp files are swept on the
  next run.
- **A read error never stops a run.** It is logged against the file and the run moves on;
  see `report errors`.
- **A file that changes between hashing and copying** is never copied stale: it goes back to
  be re-hashed on the next run.
- **A different file already at a destination path** is reported as an error, never
  overwritten.
- **A full destination** stops the run cleanly (exit code 4); free some space and run again.
- **Resuming** is always just running the same command again. Each phase picks up only rows
  in the previous state (`seen` -> `hashed` -> `copied` or `duplicate`; `pending_decision` for
  unknown extensions; `error` for read failures).
- **Non-UTF-8 and odd Windows-1252 names** survive: paths are stored as raw bytes alongside a
  decoded form.

### Not in v1

Imaging and recovering failing drives (ddrescue), deleting near-duplicates such as resized
photos, content-aware classification, and applications/OS files/settings. Cheap image labels
(photo, scan, screenshot, likely document) and a perceptual-hash near-duplicate report are
planned but not built.

## Development and testing

Everything runs in the dev container; Python 3.12, dependencies managed by Poetry
(`pyproject.toml` and `poetry.lock` are committed).

```bash
docker compose run --rm dev pytest -m "unit or component"     # fast
docker compose run --rm dev sh -c "ruff check . && mypy"      # lint and types
docker compose run --rm dev-root pytest                       # everything, incl. loop devices
```

| Layer | Marker | Covers | Needs |
|---|---|---|---|
| Unit | `unit` | rule matching, path stripping, scoring, NTFS names, SMART rules, config read/write | nothing |
| Component | `component` | catalog, copy-and-verify, WSL2 catalog sync, prompt queue, reports, mirror | temp dirs |
| System | `system` | the real CLI as a subprocess: crashes, prompts, `intake.sh` against stub tools | nothing |
| Image mocks | `image` | real NTFS/exFAT/FAT32/ext4 images, device-mapper read errors, full destination | root, loop devices (`dev-root`) |
| Container | `container` | builds the runtime image and runs a drive through it | Docker on the host |

Image-mock tests skip with a clear reason when not run as root. Mock sources are generated at
test time from specs in `tests/fixtures/devices/*.yaml` with seeded content, so nothing large
is committed, and no real names, paths or files from your drives belong in this repository.

**Crash testing.** The code marks named checkpoints (`scan.batch_commit`, `hash.after_hash`,
`copy.after_temp_write`, `copy.after_fsync`, `copy.after_rename`, `catalog.after_commit`,
`catalog.sync_copy`, and `sync.*` for the mirror). Setting
`CONSOLIDATE_FAULT=<checkpoint>:<n>:<kill|sigint|raise>` fires an action at the nth hit. The
system tests kill or interrupt the CLI at every checkpoint, resume, and require the result
(files and catalog) to equal an uninterrupted run, with no leftover temp files and sources
byte-identical. Seeded random kills do the same at arbitrary moments.

Test-only environment variables: `CONSOLIDATE_FAULT`, `CONSOLIDATE_THROTTLE` (seconds to
sleep per file), `CONSOLIDATE_PROGRESS_EVERY`, `CONSOLIDATE_BATCH` (commit batch size).

**CI** (`.github/workflows/ci.yml`) runs on every push to `main` and every pull request:
`lint` (ruff, mypy), `unit` (unit and component tests on Ubuntu and Windows), `system` (the
system and image-mock tests as root on Ubuntu) and `container` (builds the runtime image and
runs the smoke test).

### Layout

```
src/drivecon/
  cli.py          commands            catalog.py     SQLite schema, batches, WSL2 sync
  rules.py        classification      config.py      TOML load / write-back
  scan.py         the walk            session.py     scan + prompts + hash worker
  pending.py      unknown extensions  hashing.py     BLAKE3 hash worker
  paths.py        merge, NTFS names   placement.py   destination paths, conflicts
  copier.py       copy + verify       mirror.py      secondary drive
  intake.py       intake sheet        smart.py       SMART assessment
  reports.py      results, where      photos.py      EXIF dates
  fault.py        crash points        mockdevices.py synthetic source trees
scripts/intake.sh host-side intake helper
config/           file-types.toml, paths.toml
tests/            unit, component, system, fixtures
```
