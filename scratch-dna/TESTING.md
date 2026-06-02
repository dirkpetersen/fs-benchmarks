# Testing

All test suites live in `scratch-dna/`. Run them from that directory.

## Go (`scratch-dna_test.go`)

Requires Go 1.22+. The test builds the binary once via `TestMain` and exercises it as a subprocess.

```bash
go test -v -timeout 120s scratch-dna.go scratch-dna_test.go
```

Note: pass the source files explicitly (not `./...`) because `go test` refuses to mix Go and C sources in the same package without CGO.

## C (`test-scratch-dna-c.sh`)

Requires `gcc`, `python3` (for JSON validation), and `md5sum`.

```bash
bash test-scratch-dna-c.sh
```

The script compiles `scratch-dna.c` into a temp binary, runs all assertions, and prints a `PASS / FAIL` summary.

## Python orchestrator (`test_run_scratch_dna.py`)

Requires Python 3.8+ and the C binary to be buildable (the end-to-end tests compile it). Uses only the standard library.

```bash
python3 -m unittest test_run_scratch_dna -v
# or, if pytest is available:
python3 -m pytest test_run_scratch_dna.py -v
```

## What each suite covers

| Test | Go | C | Python |
|---|---|---|---|
| stdout is pure JSON | ✓ | ✓ | ✓ (e2e) |
| human chatter on stderr | ✓ | ✓ | - |
| exact JSON key set | ✓ | ✓ | ✓ (e2e) |
| file count accuracy (on-disk == JSON) | ✓ | ✓ | ✓ (e2e) |
| directory layout `host/parent/leaf/` | ✓ | ✓ | - |
| `--unique` all files distinct | ✓ | ✓ | - |
| without `--unique` same-size files identical | ✓ | ✓ | - |
| long flags work (`--parallel`, `--unique`) | ✓ | ✓ | - |
| argument validation / bad input → non-zero exit | ✓ | ✓ | ✓ (parse_args) |
| `parse_remote` accepts `[user@]host:/path` | - | - | ✓ |
| `parse_remote` rejects local/relative paths | - | - | ✓ |
| SVG graph is well-formed XML | - | - | ✓ |
| SVG multi-host legend | - | - | ✓ |
| orchestrator `inst-NNNN/` subfolders | - | - | ✓ (e2e) |
| orchestrator aggregated JSON | - | - | ✓ (e2e) |
