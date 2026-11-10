# Copilot instructions for this repository

## Repository purpose

This repo is a Linux kernel analysis pipeline, not a typical application project. It compares Rocky/RHEL kernel diffs against upstream mainline Linux commits to identify which upstream fixes are missing from the Rocky 9.4 tree.

The core flow is:

1. `step1-find_diffs.sh`: compare two local kernel trees and export per-file diffs into `kernel_diffs/*.patch`
2. `step2-generate_upstream_db.py`: build an upstream commit fingerprint database (`upstream_indexed.txt`)
3. `step3-clean_patches.py`: strip matching upstream hunks out of the diff set and keep only likely Rocky-private changes
4. `find_missing_fixes.py`: scan the latest Linux history for `Fixes:` references and report fixes that are missing from the matched upstream set

## Build, test, and lint commands

There is no formal project build/test/lint harness in this repository. The practical validation is running the pipeline scripts directly.

Use the script entry points below:

- `bash step1-find_diffs.sh`
  - Compares `centos-stream-9/` against `linux-5.14.0-427.28.1.el9_4/`
  - Writes per-file patches under `kernel_diffs/`

- `python3 step2-generate_upstream_db.py [--until 2024-05-09]`
  - Builds `upstream_indexed.txt` from upstream Linux commits before the Rocky 9.4 GA cutoff
  - The default cutoff is `2024-05-09` to avoid false positives from commits that landed after GA

- `python3 step3-clean_patches.py [--dry-run --limit N --verbose]`
  - Best focused validation when checking a single patch or a small subset
  - Recommended quick check: `python3 step3-clean_patches.py --dry-run --limit 1 --verbose`
  - The script defaults to `--match-sides plus` and `--min-match-ratio 0.8` for a conservative Rocky-only match

- `python3 find_missing_fixes.py`
  - Checks `kernel_diffs/upstream_matched/*.patch` against Linux `Fixes:` metadata
  - Defaults to pulling the Linux repo with `git pull --ff-only` unless `--no-pull` is used

- `python3 <script> --help`
  - Useful anytime you need the exact supported flags for a script without reading the whole file

The repository is data-heavy and filesystem-dependent; there is no generic `make test`, `pytest`, or `npm` workflow to run here.

## High-level architecture

### 1) Diff extraction and normalization

`step1-find_diffs.sh` is a shell pipeline that:

- compares two local trees under `/home/gjiang/sources/`
- excludes a large set of directories and files via `EXCLUDE_DIRS`
- emits one patch file per changed file into `kernel_diffs/`
- normalizes file names by replacing `/` with `_` so the output matches filenames like `arch_x86_...patch`

This script is intentionally opinionated: it only compares a few target architectures (`arch/x86`, `arch/arm64`) and skips a large set of noisy subsystems.

### 2) Upstream fingerprint database

`step2-generate_upstream_db.py` walks upstream Linux history between `v5.14` and the Rocky 9.4 cutoff and extracts hashed fingerprints from added lines in each commit. The result is a large flat text index (`upstream_indexed.txt`) keyed by hash and commit ID.

The database is intentionally very large and is meant to be rebuilt only when the upstream cutoff or commit range changes.

### 3) Rocky-private patch cleanup

`step3-clean_patches.py` is the main filtering step. It matches real upstream commit lines against `upstream_indexed.txt` and then rejects weak matches by applying heuristics:

- only `+` lines are considered by default (`--match-sides plus`)
- a candidate commit must have enough of its added lines present in the Rocky 9.4 base tree (`--min-match-ratio 0.8`)
- a secondary verification step ensures the triggering diff lines also appear in the base tree (`--verify-ratio 0.5`)

This logic is designed to avoid false positives from generic code lines that happen to repeat in unrelated upstream commits.

### 4) Missing-fix reporting

`find_missing_fixes.py` does the final reverse-check. It:

- reads `kernel_diffs/upstream_matched/*.patch` to collect the matched upstream commits
- scans the upstream Linux repo for `Fixes:` references
- identifies fix commits that were not part of the matched set
- optionally excludes fixes that are already present in the Rocky 9.4 base tree, based on a reverse-apply or line-presence heuristic

This script is the reporting layer: it produces `.tsv` and `.sha1` outputs summarizing missing upstream fixes.

## Key conventions and repo-specific patterns

- The diff direction is intentionally `centos-stream-9 -> linux-5.14.0-427.28.1.el9_4`; in the code comments, `+` lines represent base-only content and `-` lines represent CentOS-only content.
- Default matching is conservative: `step3-clean_patches.py` uses `--match-sides plus`, not `both`, because matching on the wrong side can produce false positives.
- The cutoff for upstream scanning is a real product assumption: Rocky 9.4 GA is treated as `2024-05-09`, and commits after that date are intentionally excluded from the upstream fingerprint database.
- This repo expects real local source trees at fixed paths such as:
  - `/home/gjiang/sources/centos-stream-9`
  - `/home/gjiang/sources/linux`
  - `/home/gjiang/sources/linux-5.14.0-427.28.1.el9_4`
- The scripts operate on generated files and large indexes (`upstream_indexed.txt`, `kernel_diffs/`, `upstream_matched/`), so they are designed around local disk and shell workflows rather than package installs or a web app.
- When debugging a single patch or a small run, prefer `--limit N` and `--dry-run` instead of running the full dataset.

## Existing guidance incorporated from repo docs

- README text in this repository is the primary source of workflow intent; follow the script sequence and the semantic meaning of the diff direction.
- This repo does not currently include an AGENTS.md, CLAUDE.md, .cursorrules, .windsurfrules, .clinerules, or other assistant config files to integrate with.
