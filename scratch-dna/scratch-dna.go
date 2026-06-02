package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math/rand/v2"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Directory fan-out limits, so no single folder holds an unwieldy number of
// entries: each leaf subdirectory holds up to filesPerDir files, and each
// parent directory holds up to subdirsPerDir leaf subdirectories.
const (
	filesPerDir   = 10000
	subdirsPerDir = 100
)

// stampStride is how often a uniqueness tag is written into the buffer in
// --unique mode: one 8-byte tag every stampStride bytes. 4 KiB matches common
// dedup/compression block boundaries, so every such block differs across files.
const stampStride = 4096

type config struct {
	parallel int
	verbose  bool
	timeout  int
	sync     bool
	unique   bool
}

func main() {
	// Configure logging with more details
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds | log.Lshortfile)

	cfg := parseFlags()
	args := flag.Args()

	if len(args) != 4 {
		flag.Usage()
		os.Exit(1)
	}

	nFlag := mustPositiveInt(args[0], "number-of-files")
	sFlag := mustPositiveInt(args[1], "file-size-bytes")
	mFlag := mustPositiveInt(args[2], "random-file-size-multiplier")
	dFlag := args[3]

	// Validate directory exists and is writable
	if info, err := os.Stat(dFlag); err != nil {
		log.Fatalf("Directory error: %v", err)
	} else if !info.IsDir() {
		log.Fatalf("Path is not a directory: %s", dFlag)
	}

	if cfg.parallel < 1 {
		log.Fatalf("Parallel workers must be at least 1, got: %d", cfg.parallel)
	}

	// Generate the random DNA sequence once; it is shared read-only by all workers.
	out := genstring(sFlag)

	// Use this host's name as a subdirectory under the target path so that
	// multiple hosts writing to a shared filesystem do not step on each other.
	hostname, err := os.Hostname()
	if err != nil {
		log.Printf("Warning: could not get hostname: %v, using 'dna'", err)
		hostname = "dna"
	}

	// Plan the directory layout so no single folder holds too many entries:
	//   <target>/<hostname>/<random-parent>/<random-sub>/<file>
	// Each leaf subdirectory holds up to filesPerDir files; each parent holds up
	// to subdirsPerDir leaf subdirectories. All bucket directories are created
	// up front (single-threaded) so workers never race to mkdir; a worker maps
	// its file index to a bucket with bucketFor() and looks up the path.
	outDir := filepath.Join(dFlag, hostname)
	bucketDirs := planBuckets(outDir, nFlag)

	// Context with timeout to prevent hanging.
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(cfg.timeout)*time.Second)
	defer cancel()

	fmt.Fprintf(os.Stderr, "Writing %d files with filesizes between %.1f MB and %.1f MB...\n",
		nFlag, float64(sFlag)/1048576, float64(sFlag)/1048576*float64(mFlag))
	fmt.Fprintf(os.Stderr, "Using %d parallel workers\n\n", cfg.parallel)

	var nfiles, nbytes, errCount atomic.Int64
	start := time.Now()

	// Fixed worker pool: launch exactly cfg.parallel workers that consume file
	// indices from a job channel. This bounds goroutine count regardless of how
	// many files are requested (the old version spawned one goroutine per file).
	jobs := make(chan int, cfg.parallel*2)
	var wg sync.WaitGroup
	for w := 0; w < cfg.parallel; w++ {
		wg.Add(1)
		// Per-worker RNG avoids contention on the global, concurrency-safe RNG.
		seed := rand.NewPCG(uint64(w+1), uint64(time.Now().UnixNano()))
		// In --unique mode each worker stamps the buffer in place, so it needs a
		// private writable copy; otherwise all workers share the read-only one.
		buf := out
		if cfg.unique {
			buf = append([]byte(nil), out...)
		}
		go func(rng *rand.Rand, buf []byte) {
			defer wg.Done()
			for idx := range jobs {
				if ctx.Err() != nil {
					return
				}
				wrote, err := writeFile(rng, idx, buf, bucketDirs[bucketFor(idx)], hostname, mFlag, cfg.sync, cfg.unique)
				if err != nil {
					log.Printf("Error: %v", err)
					errCount.Add(1)
					continue
				}
				nfiles.Add(1)
				nbytes.Add(wrote)
			}
		}(rand.New(seed), buf)
	}

	// Feed jobs; stop early if the context is cancelled (timeout).
	go func() {
		defer close(jobs)
		for x := 1; x <= nFlag; x++ {
			select {
			case jobs <- x:
			case <-ctx.Done():
				return
			}
		}
	}()

	// Signal completion when all workers have drained the job channel.
	doneCh := make(chan struct{})
	go func() {
		wg.Wait()
		close(doneCh)
	}()

	// Optional live progress ticker.
	var tick <-chan time.Time
	if cfg.verbose {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		tick = ticker.C
	}

	for {
		select {
		case <-doneCh:
			errs := errCount.Load()
			printDiskUsage(nfiles.Load(), nbytes.Load(), start)
			if errs > 0 {
				log.Printf("WARNING: completed with %d errors", errs)
			}
			emitResult(nfiles.Load(), nbytes.Load(), start, int64(nFlag), errs, false)
			if errs > 0 {
				os.Exit(1)
			}
			return
		case <-ctx.Done():
			log.Printf("ERROR: operation timed out after %d seconds", cfg.timeout)
			log.Printf("Files completed: %d/%d", nfiles.Load(), nFlag)
			printDiskUsage(nfiles.Load(), nbytes.Load(), start)
			emitResult(nfiles.Load(), nbytes.Load(), start, int64(nFlag), errCount.Load(), true)
			os.Exit(1)
		case <-tick:
			printProgress(nfiles.Load(), nbytes.Load(), start, int64(nFlag))
		}
	}
}

// parseFlags registers each option under both a short and a long name bound to
// the same variable, so e.g. -p and --parallel are interchangeable.
func parseFlags() config {
	var cfg config

	flag.IntVar(&cfg.parallel, "p", 1, "number of concurrent file writers (short for --parallel)")
	flag.IntVar(&cfg.parallel, "parallel", 1, "number of concurrent file writers")

	flag.BoolVar(&cfg.verbose, "v", false, "verbose: print progress every second (short for --verbose)")
	flag.BoolVar(&cfg.verbose, "verbose", false, "verbose: print progress every second")

	flag.IntVar(&cfg.timeout, "t", 600, "timeout in seconds for the entire operation (short for --timeout)")
	flag.IntVar(&cfg.timeout, "timeout", 600, "timeout in seconds for the entire operation")

	flag.BoolVar(&cfg.sync, "s", false, "fsync() after each file write (slower, ensures data on disk) (short for --sync)")
	flag.BoolVar(&cfg.sync, "sync", false, "fsync() after each file write (slower, ensures data on disk)")

	flag.BoolVar(&cfg.unique, "u", false, "make each block unique to defeat dedup/cache (short for --unique)")
	flag.BoolVar(&cfg.unique, "unique", false, "make each block unique to defeat dedup/cache")

	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "Error! 4 positional arguments required.\n")
		fmt.Fprintf(os.Stderr, "\nUsage: %s [options] <number-of-files> <file-size-bytes> <random-file-size-multiplier> <writable-directory>\n", os.Args[0])
		fmt.Fprintf(os.Stderr, "\nOptions (short and long forms are equivalent):\n")
		fmt.Fprintf(os.Stderr, "  -p, --parallel <n>   number of concurrent file writers (default 1)\n")
		fmt.Fprintf(os.Stderr, "  -v, --verbose        print progress every second\n")
		fmt.Fprintf(os.Stderr, "  -t, --timeout <sec>  timeout for the whole operation (default 600)\n")
		fmt.Fprintf(os.Stderr, "  -s, --sync           fsync() after each file write\n")
		fmt.Fprintf(os.Stderr, "  -u, --unique         stamp a unique tag every 4 KiB so no two blocks repeat\n")
		fmt.Fprintf(os.Stderr, "\nExample: %s -v -p 8 1024 10485760 1 /tmp\n", os.Args[0])
		fmt.Fprintf(os.Stderr, "         %s --verbose --parallel 8 1024 10485760 1 /tmp\n\n", os.Args[0])
	}

	flag.Parse()
	return cfg
}

func mustPositiveInt(s, name string) int {
	v, err := strconv.Atoi(s)
	if err != nil || v < 1 {
		log.Fatalf("Invalid %s: %q (must be a positive integer)", name, s)
	}
	return v
}

// genstring builds a random DNA sequence of the given size. It draws 32 bases
// from each 64-bit random word (2 bits per base) instead of calling the RNG
// once per byte.
func genstring(size int) []byte {
	fmt.Fprintf(os.Stderr, "Building random DNA sequence of %.1f MB...\n\n", float64(size)/1048576)
	dnachars := [4]byte{'G', 'A', 'T', 'C'}
	dna := make([]byte, size)
	var r uint64
	var bits int
	for i := 0; i < size; i++ {
		if bits == 0 {
			r = rand.Uint64()
			bits = 64
		}
		dna[i] = dnachars[r&3]
		r >>= 2
		bits -= 2
	}
	return dna
}

// bucketFor maps a 1-based file index to its leaf-bucket number. Files
// 1..filesPerDir go in bucket 0, the next filesPerDir in bucket 1, and so on.
func bucketFor(idx int) int {
	return (idx - 1) / filesPerDir
}

// planBuckets creates, single-threaded and up front, every leaf directory
// needed to hold nFiles files under base, and returns them indexed by bucket
// number. Layout is base/<random-parent>/<random-sub>; a new random parent is
// started after every subdirsPerDir leaf subdirectories. Creating all bucket
// directories before any worker starts means workers never race on mkdir.
func planBuckets(base string, nFiles int) []string {
	if err := os.MkdirAll(base, 0o755); err != nil {
		log.Fatalf("Failed to create output directory %s: %v", base, err)
	}

	nBuckets := (nFiles + filesPerDir - 1) / filesPerDir
	if nBuckets < 1 {
		nBuckets = 1
	}

	dirs := make([]string, nBuckets)
	var parent string
	for b := 0; b < nBuckets; b++ {
		if b%subdirsPerDir == 0 {
			parent = filepath.Join(base, randName())
			if err := os.MkdirAll(parent, 0o755); err != nil {
				log.Fatalf("Failed to create directory %s: %v", parent, err)
			}
		}
		leaf := filepath.Join(parent, randName())
		if err := os.MkdirAll(leaf, 0o755); err != nil {
			log.Fatalf("Failed to create directory %s: %v", leaf, err)
		}
		dirs[b] = leaf
	}
	return dirs
}

// randName returns a random 16-hex-character directory name.
func randName() string {
	const hex = "0123456789abcdef"
	var b [16]byte
	r := rand.Uint64()
	for i := range b {
		b[i] = hex[r&0xf]
		r >>= 4
		if i == 7 {
			r = rand.Uint64()
		}
	}
	return string(b[:])
}

// stamp writes an 8-byte uniqueness tag every stampStride bytes of buf. The tag
// mixes the file index, the repetition (block) number, and the byte offset, so
// every stampStride-aligned block is distinct across the whole run while only a
// few bytes per block are touched — cost is negligible against the write() of
// the surrounding block. buf must be a buffer the caller exclusively owns.
func stamp(buf []byte, fileIdx, block int) {
	base := uint64(fileIdx)<<20 ^ uint64(block)<<4
	for off := 0; off+8 <= len(buf); off += stampStride {
		binary.LittleEndian.PutUint64(buf[off:], base^uint64(off))
	}
}

// writeFile writes the DNA buffer a random number of times (1..mFlag) to a new
// file. It writes the buffer directly to the *os.File without an intermediate
// bufio buffer, since each write is already a large block. When unique is set,
// buf is the worker's private copy and is stamped in place before each write so
// no two blocks across the run are identical (see stamp).
func writeFile(rng *rand.Rand, count int, buf []byte, dir, hostname string, mFlag int, doSync, unique bool) (int64, error) {
	multiple := rng.IntN(mFlag) + 1
	filename := filepath.Join(dir, hostname+"-"+strconv.Itoa(count)+"-"+strconv.Itoa(multiple)+".txt")

	f, err := os.Create(filename)
	if err != nil {
		return 0, fmt.Errorf("create %s: %w", filename, err)
	}

	var written int64
	for j := 0; j < multiple; j++ {
		if unique {
			stamp(buf, count, j)
		}
		n, err := f.Write(buf)
		written += int64(n)
		if err != nil {
			f.Close()
			return written, fmt.Errorf("write %s (iteration %d/%d): %w", filename, j+1, multiple, err)
		}
	}

	if doSync {
		if err := f.Sync(); err != nil {
			f.Close()
			return written, fmt.Errorf("sync %s: %w", filename, err)
		}
	}

	if err := f.Close(); err != nil {
		return written, fmt.Errorf("close %s: %w", filename, err)
	}
	return written, nil
}

// All human-readable output goes to stderr; stdout is reserved for the final
// JSON result object (see emitResult).
func printProgress(nfiles, nbytes int64, start time.Time, total int64) {
	elapsed := time.Since(start).Seconds()
	if elapsed <= 0 {
		elapsed = 1e-9
	}
	fps := float64(nfiles) / elapsed
	tp := float64(nbytes) / elapsed
	remaining := total - nfiles
	pct := float64(nfiles) / float64(total) * 100
	fmt.Fprintf(os.Stderr, "Progress: %6.2f%% | Files: %7d/%7d | Data: %5.1f GiB | FPS: %7.0f | Throughput: %6.0f MiB/s | Remaining: %7d\n",
		pct, nfiles, total, float64(nbytes)/1073741824, fps, tp/1048576, remaining)
}

// printDiskUsage prints the human-readable final summary to stderr.
func printDiskUsage(nfiles, nbytes int64, start time.Time) {
	elapsed := time.Since(start).Seconds()
	if elapsed <= 0 {
		elapsed = 1e-9
	}
	fps := float64(nfiles) / elapsed
	tp := float64(nbytes) / elapsed
	separator := strings.Repeat("=", 80)
	fmt.Fprintf(os.Stderr, "\n%s\n", separator)
	fmt.Fprintf(os.Stderr, "Benchmark Complete!\n")
	fmt.Fprintf(os.Stderr, "%s\n", separator)
	fmt.Fprintf(os.Stderr, "Files Written:    %d\n", nfiles)
	fmt.Fprintf(os.Stderr, "Total Size:       %.2f GiB (%.2f MB)\n", float64(nbytes)/1073741824, float64(nbytes)/1048576)
	fmt.Fprintf(os.Stderr, "Elapsed Time:     %.3f seconds\n", elapsed)
	fmt.Fprintf(os.Stderr, "Avg FPS:          %.0f files/second\n", fps)
	fmt.Fprintf(os.Stderr, "Avg Throughput:   %.0f MiB/s\n", tp/1048576)
	fmt.Fprintf(os.Stderr, "%s\n", separator)
}

// result is the machine-readable summary emitted as JSON on stdout.
type result struct {
	FilesRequested  int64   `json:"files_requested"`
	FilesWritten    int64   `json:"files_written"`
	BytesWritten    int64   `json:"bytes_written"`
	TotalMB         float64 `json:"total_mb"`
	TotalGiB        float64 `json:"total_gib"`
	ElapsedSeconds  float64 `json:"elapsed_seconds"`
	FilesPerSecond  float64 `json:"files_per_second"`
	ThroughputMiBps float64 `json:"throughput_mib_per_second"`
	Errors          int64   `json:"errors"`
	TimedOut        bool    `json:"timed_out"`
}

// emitResult writes the final stats to stdout as a single JSON object. stdout
// carries only this object, so callers can pipe it straight into a parser.
func emitResult(nfiles, nbytes int64, start time.Time, total, errs int64, timedOut bool) {
	elapsed := time.Since(start).Seconds()
	if elapsed <= 0 {
		elapsed = 1e-9
	}
	res := result{
		FilesRequested:  total,
		FilesWritten:    nfiles,
		BytesWritten:    nbytes,
		TotalMB:         float64(nbytes) / 1048576,
		TotalGiB:        float64(nbytes) / 1073741824,
		ElapsedSeconds:  elapsed,
		FilesPerSecond:  float64(nfiles) / elapsed,
		ThroughputMiBps: float64(nbytes) / elapsed / 1048576,
		Errors:          errs,
		TimedOut:        timedOut,
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(res); err != nil {
		log.Printf("Error: failed to encode JSON result: %v", err)
	}
}
