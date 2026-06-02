# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Homegrown filesystem benchmarks. The primary benchmark is `scratch-dna`, which measures write throughput by generating random DNA-sequence files (A/G/T/C characters) across a target directory. Three implementations exist in `scratch-dna/`: C, Python 3, and Go.

All three share the same positional argument contract:
```
<number-of-files> <file-size-bytes> <random-file-size-multiplier> <writable-directory>
```
The multiplier causes each file's actual size to be `file-size-bytes * random(1..multiplier)`, producing a realistic mix of file sizes.

## Output contract (Go and C)

`stdout` carries **only** a single JSON object with the final stats; all human-readable output (the build/"Writing…" messages, `-v` progress lines, the `===` summary banner, warnings, errors) goes to `stderr`. This lets callers pipe results straight into a parser, e.g. `scratch-dna ... 2>/dev/null | jq`. The JSON keys are identical across both implementations: `files_requested`, `files_written`, `bytes_written`, `total_mb`, `total_gib`, `elapsed_seconds`, `files_per_second`, `throughput_mib_per_second`, `errors`, `timed_out`. The object is emitted on both normal completion and timeout (with `timed_out: true`). The Python version does not follow this contract.

## Building and running

**Go** (most feature-complete — use as the reference implementation):
```bash
cd scratch-dna
go run scratch-dna.go -v -p 8 1024 10485760 1 /tmp
# or build first:
go build -o scratch-dna scratch-dna.go
./scratch-dna -v -p 8 1024 10485760 1 /tmp
```
Go-only flags — each has an equivalent short and long form (interchangeable, bound to the same variable):

| Short | Long | Meaning |
|-------|------|---------|
| `-p` | `--parallel` | number of concurrent file writers (default 1) |
| `-v` | `--verbose` | print live progress every second |
| `-t` | `--timeout` | timeout in seconds for the whole run (default 600) |
| `-s` | `--sync` | fsync() after each file write |
| `-u` | `--unique` | stamp a unique 8-byte tag every 4 KiB so no two blocks repeat |

**C** (behaviorally identical to the Go version — same flags, output, filenames, and worker-pool model):
```bash
cd scratch-dna
gcc -O2 -pthread -o scratch-dna scratch-dna.c
./scratch-dna -v -p 8 1024 10485760 1 /tmp
./scratch-dna --verbose --parallel 8 1024 10485760 1 /tmp
```
The C version requires `-pthread`. It accepts the same short/long flags as Go (`-p`/`--parallel`, `-v`/`--verbose`, `-t`/`--timeout`, `-s`/`--sync`, `-u`/`--unique`) and uses a fixed pool of pthreads pulling indices from a shared atomic counter, with per-thread RNG.

**Python (`scratch-dna.py`)** — the original standalone single-process generator:
```bash
cd scratch-dna
python3 scratch-dna.py 1024 10485760 1 /tmp
```
No parallelism; predates the Go/C rewrites and does not follow the directory-bucketing or JSON output contract.

**Python orchestrator (`run-scratch-dna.py`)** — runs the compiled **C** binary as multiple parallel *processes* through a bounded queue:
```bash
cd scratch-dna
gcc -O2 -pthread -o scratch-dna-c scratch-dna.c   # build the binary it drives
python3 run-scratch-dna.py -p 4 -T 4 1000 65536 2 /tmp        # 4 procs x 4 threads
python3 run-scratch-dna.py -p 8 --keep-going --disk-limit 80 1000 65536 2 /tmp
python3 run-scratch-dna.py -p 4 user@host:/mnt/data 1000 65536 2  # remote target
```
- `-p/--parallel` = concurrent C **processes**; `-T/--threads` = writer threads per process (forwarded as the C binary's own `-p`). So `-p 4 -T 4` is 16-way concurrency.
- `-j/--jobs` sets total instance count (default = `-p`); `--keep-going` loops until `--disk-limit` percent (default 80) of the disk is used.
- Each instance writes to `<root>/inst-NNNN/`. It locates the C binary via `--bin`, `$SCRATCH_DNA_C`, `PATH`, or alongside the script.
- **Remote**: if the root dir is `[user@]host:/abs/path`, it `scp`s the binaries + this script to `~/bin` on that host and runs there over SSH, capturing the JSON back.
- Same output contract: aggregated JSON summary on stdout, human text on stderr. Writes an SVG performance bar chart (per-instance throughput) to `<root>/scratch-dna-results.svg` unless `--no-graph`.

## Architecture notes

- **Directory layout** (Go and C, identical): files go under `<target>/<hostname>/<random-parent>/<random-leaf>/`. Using the bare hostname as the top-level subdirectory keeps multiple hosts on a shared filesystem from stepping on each other. To avoid overfilling any one directory, each leaf holds at most `filesPerDir` (10000) files and each parent holds at most `subdirsPerDir` (100) leaves; a new randomly-named directory is started when a limit is reached. All bucket directories are created single-threaded up front (`planBuckets` / `plan_buckets`), so a file index maps to its directory with `bucketFor(idx) = (idx-1)/filesPerDir` and workers never race on `mkdir`. (Python still uses the old flat `<hostname>/` layout.)
- File naming: `<hostname>-<index>-<multiplier>.txt` — the multiplier value is embedded in the filename so it can be verified after the fact.
- The Go and C versions both use a **fixed pool of `-p` workers** pulling file indices from a shared queue/counter, so memory stays O(workers) rather than O(files). Each worker has its own RNG to avoid contention, and counts are tracked with atomic counters incremented directly in the workers.
- **`-u`/`--unique`**: the DNA buffer is generated once and normally written verbatim to every file, which lets the page cache and filesystem/storage dedup collapse the work and inflate throughput numbers. In unique mode each worker gets a private copy of the buffer and `stamp()` writes an 8-byte tag (derived from file index, block number, and offset) every `stampStride` (4096) bytes before each block write — defeating dedup while touching only a few bytes per block. Expect throughput to drop to the real distinct-data rate.
- There is no `go.mod` — the Go file is a self-contained single-file program with no external dependencies. Build/vet it with the explicit filename: `go build scratch-dna.go` / `go vet scratch-dna.go`.

## Releases / CI

`.github/workflows/release.yml` builds release binaries on any `v*` tag push. It matrix-builds for linux **amd64** and **arm64** (arm64 C via the `gcc-aarch64-linux-gnu` cross-compiler; Go cross-compiles natively), producing `scratch-dna-go-linux-<arch>` and `scratch-dna-c-linux-<arch>` (C is statically linked), then publishes them with checksums to a GitHub Release via the `gh` CLI. To cut a release: `git tag v1.0.0 && git push origin v1.0.0`.
