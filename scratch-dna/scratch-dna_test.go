package main

// End-to-end tests for the scratch-dna Go benchmark.
//
// The program is a `package main` whose logic lives almost entirely in main(),
// so these tests build the binary once (TestMain) into a temp dir and exercise
// it as a subprocess, asserting on the stdout JSON, stderr separation, the
// on-disk file layout, and the --unique behavior.
//
// Run with:  go test -v scratch-dna.go scratch-dna_test.go   (from the scratch-dna directory)

import (
	"bytes"
	"crypto/md5"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// binPath is the compiled benchmark binary, set up by TestMain.
var binPath string

// expectedJSONKeys is the exact stdout JSON contract.
var expectedJSONKeys = []string{
	"files_requested", "files_written", "bytes_written", "total_mb", "total_gib",
	"elapsed_seconds", "files_per_second", "throughput_mib_per_second",
	"errors", "timed_out",
}

func TestMain(m *testing.M) {
	tmp, err := os.MkdirTemp("", "scratch-dna-gobuild-")
	if err != nil {
		fmt.Fprintf(os.Stderr, "failed to make temp build dir: %v\n", err)
		os.Exit(1)
	}
	binPath = filepath.Join(tmp, "scratch-dna-go-test")
	build := exec.Command("go", "build", "-o", binPath, "scratch-dna.go")
	build.Stderr = os.Stderr
	if err := build.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "go build failed: %v\n", err)
		os.RemoveAll(tmp)
		os.Exit(1)
	}
	code := m.Run()
	os.RemoveAll(tmp)
	os.Exit(code)
}

// run executes the binary and returns stdout, stderr, and the exit code.
func run(t *testing.T, args ...string) (string, string, int) {
	t.Helper()
	cmd := exec.Command(binPath, args...)
	var out, errBuf bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &errBuf
	err := cmd.Run()
	code := 0
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			code = ee.ExitCode()
		} else {
			t.Fatalf("failed to run binary: %v", err)
		}
	}
	return out.String(), errBuf.String(), code
}

// countTxtFiles returns all *.txt files found under root.
func countTxtFiles(t *testing.T, root string) []string {
	t.Helper()
	var files []string
	err := filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if !info.IsDir() && strings.HasSuffix(p, ".txt") {
			files = append(files, p)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walk failed: %v", err)
	}
	return files
}

// parseJSON decodes stdout into a generic map and asserts it is the only thing
// printed (single JSON object).
func parseJSON(t *testing.T, stdout string) map[string]interface{} {
	t.Helper()
	dec := json.NewDecoder(strings.NewReader(stdout))
	var m map[string]interface{}
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("stdout is not a JSON object: %v\nstdout was:\n%s", err, stdout)
	}
	// Ensure there is no trailing second object.
	var extra interface{}
	if err := dec.Decode(&extra); err == nil {
		t.Fatalf("stdout contained more than one JSON value:\n%s", stdout)
	}
	return m
}

func TestBasicRunAndJSONContract(t *testing.T) {
	dir := t.TempDir()
	const n = 12
	stdout, stderr, code := run(t, strconv.Itoa(n), "1024", "1", dir)
	if code != 0 {
		t.Fatalf("expected exit 0, got %d\nstderr:\n%s", code, stderr)
	}

	m := parseJSON(t, stdout)

	// Exact key set.
	if len(m) != len(expectedJSONKeys) {
		t.Errorf("JSON has %d keys, want %d: %v", len(m), len(expectedJSONKeys), keysOf(m))
	}
	for _, k := range expectedJSONKeys {
		if _, ok := m[k]; !ok {
			t.Errorf("missing JSON key %q", k)
		}
	}

	if got := int(m["files_requested"].(float64)); got != n {
		t.Errorf("files_requested = %d, want %d", got, n)
	}
	if got := int(m["files_written"].(float64)); got != n {
		t.Errorf("files_written = %d, want %d", got, n)
	}
	if got := int(m["errors"].(float64)); got != 0 {
		t.Errorf("errors = %d, want 0", got)
	}
	if m["timed_out"].(bool) {
		t.Errorf("timed_out = true, want false")
	}

	// File-count accuracy: files on disk == files_written == requested.
	files := countTxtFiles(t, dir)
	if len(files) != n {
		t.Errorf("found %d .txt files on disk, want %d", len(files), n)
	}
}

func TestStdoutIsOnlyJSON(t *testing.T) {
	dir := t.TempDir()
	stdout, stderr, code := run(t, "-v", "3", "2048", "2", dir)
	if code != 0 {
		t.Fatalf("expected exit 0, got %d\nstderr:\n%s", code, stderr)
	}
	// Human-readable text must be on stderr.
	if !strings.Contains(stderr, "Benchmark Complete") {
		t.Errorf("expected human-readable summary on stderr, got:\n%s", stderr)
	}
	// stdout must parse cleanly as a single JSON object with no extra text.
	trimmed := strings.TrimSpace(stdout)
	if !strings.HasPrefix(trimmed, "{") || !strings.HasSuffix(trimmed, "}") {
		t.Errorf("stdout is not exactly one JSON object:\n%s", stdout)
	}
	parseJSON(t, stdout)
}

func TestDirectoryLayout(t *testing.T) {
	// Default: <dir>/<hostname>/w<N>/<parent>/<leaf>/<file>, one w<N> per worker.
	dir := t.TempDir()
	const n, p = 40, 3
	_, stderr, code := run(t, "-p", strconv.Itoa(p), strconv.Itoa(n), "512", "3", dir)
	if code != 0 {
		t.Fatalf("exit %d\nstderr:\n%s", code, stderr)
	}
	checkLayout(t, dir, n, true)

	hostname, _ := os.Hostname()
	entries, err := os.ReadDir(filepath.Join(dir, hostname))
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, e := range entries {
		got = append(got, e.Name())
	}
	if strings.Join(got, ",") != "w1,w2,w3" {
		t.Errorf("worker dirs = %v, want [w1 w2 w3]", got)
	}
}

func TestOneDirLayout(t *testing.T) {
	// --one-dir: <dir>/<hostname>/<parent>/<leaf>/<file>, shared by all workers.
	dir := t.TempDir()
	const n = 40
	_, stderr, code := run(t, "--one-dir", "-p", "3", strconv.Itoa(n), "512", "3", dir)
	if code != 0 {
		t.Fatalf("exit %d\nstderr:\n%s", code, stderr)
	}
	checkLayout(t, dir, n, false)
}

// checkLayout asserts every file sits at <dir>/<hostname>/[w<N>/]<parent>/<leaf>/
// <hostname>-<index>-<mult>.txt, with 16-hex-char parent and leaf names.
func checkLayout(t *testing.T, dir string, n int, perWorker bool) {
	t.Helper()
	hostname, _ := os.Hostname()

	files := countTxtFiles(t, dir)
	if len(files) != n {
		t.Fatalf("found %d files, want %d", len(files), n)
	}
	depth := 4
	if perWorker {
		depth = 5
	}
	for _, f := range files {
		rel, err := filepath.Rel(dir, f)
		if err != nil {
			t.Fatal(err)
		}
		parts := strings.Split(rel, string(os.PathSeparator))
		if len(parts) != depth {
			t.Errorf("unexpected path depth for %q: %v", rel, parts)
			continue
		}
		if parts[0] != hostname {
			t.Errorf("first path component = %q, want hostname %q", parts[0], hostname)
		}
		if perWorker {
			if w, err := strconv.Atoi(strings.TrimPrefix(parts[1], "w")); err != nil || !strings.HasPrefix(parts[1], "w") || w < 1 {
				t.Errorf("worker dir component %q is not w<N>", parts[1])
			}
		}
		// parent and leaf should be 16-hex-char names.
		for _, comp := range parts[depth-3 : depth-1] {
			if len(comp) != 16 || !isHex(comp) {
				t.Errorf("dir component %q is not 16 hex chars", comp)
			}
		}
		// Filename: <hostname>-<index>-<multiplier>.txt
		base := parts[depth-1]
		if !strings.HasPrefix(base, hostname+"-") || !strings.HasSuffix(base, ".txt") {
			t.Errorf("filename %q does not match <hostname>-<index>-<mult>.txt", base)
		}
	}
}

func TestNonUniqueFilesAreIdentical(t *testing.T) {
	dir := t.TempDir()
	// multiplier 1 -> every file is exactly one DNA buffer, so all same size.
	_, stderr, code := run(t, "8", "4096", "1", dir)
	if code != 0 {
		t.Fatalf("exit %d\nstderr:\n%s", code, stderr)
	}
	hashes := hashesOf(t, countTxtFiles(t, dir))
	if len(uniqueStrings(hashes)) != 1 {
		t.Errorf("without -u, all same-size files should be byte-identical; got %d distinct md5s",
			len(uniqueStrings(hashes)))
	}
}

func TestUniqueFilesAllDistinct(t *testing.T) {
	dir := t.TempDir()
	const n = 8
	// -u with multiplier 1 -> all files same size but every one must differ.
	_, stderr, code := run(t, "-u", strconv.Itoa(n), "8192", "1", dir)
	if code != 0 {
		t.Fatalf("exit %d\nstderr:\n%s", code, stderr)
	}
	files := countTxtFiles(t, dir)
	if len(files) != n {
		t.Fatalf("found %d files, want %d", len(files), n)
	}
	hashes := hashesOf(t, files)
	if len(uniqueStrings(hashes)) != n {
		t.Errorf("with -u every file must have a distinct md5; got %d distinct of %d files",
			len(uniqueStrings(hashes)), n)
	}
}

func TestLongFlagsEquivalent(t *testing.T) {
	dir := t.TempDir()
	// --unique and --parallel long forms should work like their short forms.
	_, stderr, code := run(t, "--unique", "--parallel", "2", "6", "4096", "1", dir)
	if code != 0 {
		t.Fatalf("exit %d\nstderr:\n%s", code, stderr)
	}
	files := countTxtFiles(t, dir)
	if len(files) != 6 {
		t.Fatalf("found %d files, want 6", len(files))
	}
	if len(uniqueStrings(hashesOf(t, files))) != 6 {
		t.Errorf("--unique should produce distinct files")
	}
}

func TestValidationErrors(t *testing.T) {
	good := t.TempDir()
	cases := []struct {
		name string
		args []string
	}{
		{"zero files", []string{"0", "1024", "1", good}},
		{"negative files", []string{"-5", "1024", "1", good}},
		{"non-integer size", []string{"10", "abc", "1", good}},
		{"zero multiplier", []string{"10", "1024", "0", good}},
		{"too few args", []string{"10", "1024", good}},
		{"nonexistent dir", []string{"10", "1024", "1", filepath.Join(good, "does-not-exist")}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			stdout, stderr, code := run(t, c.args...)
			if code == 0 {
				t.Errorf("expected non-zero exit, got 0 (stdout=%q)", stdout)
			}
			if strings.TrimSpace(stderr) == "" {
				t.Errorf("expected an error message on stderr")
			}
		})
	}
}

func TestTimedOutFieldFalseOnSuccess(t *testing.T) {
	dir := t.TempDir()
	stdout, _, code := run(t, "-t", "600", "3", "1024", "1", dir)
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	m := parseJSON(t, stdout)
	if m["timed_out"].(bool) {
		t.Errorf("timed_out should be false on a fast successful run")
	}
}

// ----- helpers -------------------------------------------------------------

func keysOf(m map[string]interface{}) []string {
	ks := make([]string, 0, len(m))
	for k := range m {
		ks = append(ks, k)
	}
	return ks
}

func isHex(s string) bool {
	for _, r := range s {
		if !((r >= '0' && r <= '9') || (r >= 'a' && r <= 'f')) {
			return false
		}
	}
	return true
}

func hashesOf(t *testing.T, files []string) []string {
	t.Helper()
	out := make([]string, 0, len(files))
	for _, f := range files {
		data, err := os.ReadFile(f)
		if err != nil {
			t.Fatalf("read %s: %v", f, err)
		}
		sum := md5.Sum(data)
		out = append(out, fmt.Sprintf("%x", sum))
	}
	return out
}

func uniqueStrings(in []string) []string {
	seen := map[string]struct{}{}
	var out []string
	for _, s := range in {
		if _, ok := seen[s]; !ok {
			seen[s] = struct{}{}
			out = append(out, s)
		}
	}
	return out
}
