#!/usr/bin/env bash
#
# test-scratch-dna-c.sh — build and test the C scratch-dna benchmark.
#
# Compiles scratch-dna.c, then runs a battery of assertions covering:
#   - the stdout JSON contract (valid JSON via python3 -m json.tool, exact keys)
#   - stdout/stderr separation (only JSON on stdout, human text on stderr)
#   - on-disk file-count accuracy (== files_written == requested)
#   - the directory layout (<dir>/<host>/<parent>/<leaf>/<host>-<idx>-<mult>.txt)
#   - --unique: every file has a distinct md5; without -u, same-size files match
#   - argument validation (non-positive / non-integer / missing dir -> non-zero)
#   - long/short flag equivalence
#
# Exits non-zero on the first failure and prints a PASS/FAIL summary.
#
# Usage:  ./test-scratch-dna-c.sh

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/scratch-dna.c"
BIN="$SCRIPT_DIR/scratch-dna-c"

PASS=0
FAIL=0
WORK=""

cleanup() {
    [ -n "$WORK" ] && rm -rf "$WORK"
}
trap cleanup EXIT

fail() {
    echo "  FAIL: $*" >&2
    FAIL=$((FAIL + 1))
}

ok() {
    echo "  ok:   $*"
    PASS=$((PASS + 1))
}

# count_txt <dir>  -> number of *.txt files
count_txt() {
    find "$1" -type f -name '*.txt' | wc -l | tr -d ' '
}

# json_get <file> <key>  -> value (via python3, no external deps)
json_get() {
    python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$1" "$2"
}

echo "=== Building scratch-dna-c ==="
if ! gcc -O2 -pthread -o "$BIN" "$SRC"; then
    echo "BUILD FAILED" >&2
    exit 1
fi
echo "  built: $BIN"

WORK="$(mktemp -d)"
HOST="$(hostname)"

# --------------------------------------------------------------------------
echo "=== Test: basic run, JSON contract, file count ==="
D="$WORK/basic"; mkdir -p "$D"
N=12
"$BIN" "$N" 1024 1 "$D" >"$WORK/out.json" 2>"$WORK/out.err"
RC=$?
[ "$RC" -eq 0 ] && ok "exit code 0" || fail "exit code $RC"

# stdout must be valid JSON on its own
if python3 -m json.tool "$WORK/out.json" >/dev/null 2>&1; then
    ok "stdout is valid JSON"
else
    fail "stdout is not valid JSON:"; cat "$WORK/out.json" >&2
fi

# exact key set
EXPECTED_KEYS="bytes_written elapsed_seconds errors files_per_second files_requested files_written throughput_mib_per_second timed_out total_gib total_mb"
GOT_KEYS="$(python3 -c 'import json;print(" ".join(sorted(json.load(open("'"$WORK/out.json"'")).keys())))')"
if [ "$GOT_KEYS" = "$EXPECTED_KEYS" ]; then
    ok "JSON has exactly the expected keys"
else
    fail "JSON keys mismatch: got [$GOT_KEYS]"
fi

# files_written / files_requested match request
FW="$(json_get "$WORK/out.json" files_written)"
FR="$(json_get "$WORK/out.json" files_requested)"
[ "$FR" = "$N" ] && ok "files_requested == $N" || fail "files_requested=$FR"
[ "$FW" = "$N" ] && ok "files_written == $N" || fail "files_written=$FW"

# files on disk == files_written
ONDISK="$(count_txt "$D")"
[ "$ONDISK" = "$N" ] && ok "on-disk .txt count == $N" || fail "on-disk count=$ONDISK"

# errors == 0, timed_out false
ERRS="$(json_get "$WORK/out.json" errors)"
TO="$(json_get "$WORK/out.json" timed_out)"
[ "$ERRS" = "0" ] && ok "errors == 0" || fail "errors=$ERRS"
[ "$TO" = "False" ] && ok "timed_out == false" || fail "timed_out=$TO"

# --------------------------------------------------------------------------
echo "=== Test: stdout/stderr separation ==="
# Human-readable summary must be on stderr.
if grep -q "Benchmark Complete" "$WORK/out.err"; then
    ok "human-readable summary on stderr"
else
    fail "no 'Benchmark Complete' on stderr"
fi
# stdout must start with { and end with } (only the JSON object).
FIRST="$(head -c1 "$WORK/out.json")"
LAST="$(tr -d '[:space:]' < "$WORK/out.json" | tail -c1)"
[ "$FIRST" = "{" ] && [ "$LAST" = "}" ] && ok "stdout is exactly one JSON object" \
    || fail "stdout not bracketed cleanly (first=$FIRST last=$LAST)"

# --------------------------------------------------------------------------
echo "=== Test: directory layout ==="
D="$WORK/layout"; mkdir -p "$D"
"$BIN" 4 512 2 "$D" >/dev/null 2>&1
LAYOUT_OK=1
while IFS= read -r f; do
    # strip the temp root prefix -> <host>/<parent>/<leaf>/<file>
    rel="${f#"$D"/}"
    depth="$(awk -F/ '{print NF}' <<<"$rel")"
    [ "$depth" -eq 4 ] || { LAYOUT_OK=0; echo "    bad depth: $rel" >&2; }
    top="$(cut -d/ -f1 <<<"$rel")"
    [ "$top" = "$HOST" ] || { LAYOUT_OK=0; echo "    top dir not host: $top" >&2; }
    base="$(basename "$f")"
    case "$base" in
        "$HOST"-*-*.txt) : ;;
        *) LAYOUT_OK=0; echo "    bad filename: $base" >&2 ;;
    esac
done < <(find "$D" -type f -name '*.txt')
[ "$LAYOUT_OK" -eq 1 ] && ok "layout <host>/<parent>/<leaf>/<host>-<idx>-<mult>.txt" \
    || fail "directory layout mismatch"

# --------------------------------------------------------------------------
echo "=== Test: non-unique files are byte-identical ==="
D="$WORK/dup"; mkdir -p "$D"
"$BIN" 8 4096 1 "$D" >/dev/null 2>&1   # multiplier 1 => all same size
DISTINCT="$(find "$D" -type f -name '*.txt' -exec md5sum {} \; | awk '{print $1}' | sort -u | wc -l | tr -d ' ')"
[ "$DISTINCT" = "1" ] && ok "without -u, all same-size files share one md5" \
    || fail "expected 1 distinct md5, got $DISTINCT"

# --------------------------------------------------------------------------
echo "=== Test: --unique produces distinct md5 per file ==="
D="$WORK/uniq"; mkdir -p "$D"
"$BIN" -u 8 8192 1 "$D" >/dev/null 2>&1
NF="$(count_txt "$D")"
DISTINCT="$(find "$D" -type f -name '*.txt' -exec md5sum {} \; | awk '{print $1}' | sort -u | wc -l | tr -d ' ')"
[ "$DISTINCT" = "$NF" ] && [ "$NF" = "8" ] && ok "with -u every file has a distinct md5 ($DISTINCT/8)" \
    || fail "with -u expected 8 distinct md5s, got $DISTINCT of $NF"

# long form --unique behaves the same
D="$WORK/uniqlong"; mkdir -p "$D"
"$BIN" --unique --parallel 2 6 8192 1 "$D" >/dev/null 2>&1
DISTINCT="$(find "$D" -type f -name '*.txt' -exec md5sum {} \; | awk '{print $1}' | sort -u | wc -l | tr -d ' ')"
[ "$DISTINCT" = "6" ] && ok "--unique --parallel long forms work (6 distinct)" \
    || fail "long-form --unique gave $DISTINCT distinct md5s"

# --------------------------------------------------------------------------
echo "=== Test: argument validation ==="
GOOD="$WORK/valid"; mkdir -p "$GOOD"
expect_fail() {
    local desc="$1"; shift
    "$BIN" "$@" >/dev/null 2>"$WORK/verr"
    local rc=$?
    if [ "$rc" -ne 0 ] && [ -s "$WORK/verr" ]; then
        ok "rejected: $desc (rc=$rc, stderr non-empty)"
    else
        fail "$desc should error (rc=$rc, stderr size=$(wc -c <"$WORK/verr"))"
    fi
}
expect_fail "zero files"        0 1024 1 "$GOOD"
expect_fail "non-integer size"  10 abc 1 "$GOOD"
expect_fail "zero multiplier"   10 1024 0 "$GOOD"
expect_fail "too few args"      10 1024 "$GOOD"
expect_fail "nonexistent dir"   10 1024 1 "$GOOD/nope-does-not-exist"

# --------------------------------------------------------------------------
echo ""
echo "=================================================="
echo "  PASS: $PASS    FAIL: $FAIL"
echo "=================================================="
if [ "$FAIL" -ne 0 ]; then
    echo "RESULT: FAIL"
    exit 1
fi
echo "RESULT: PASS"
exit 0
