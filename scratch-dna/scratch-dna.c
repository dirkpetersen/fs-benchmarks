/*
 * scratch-dna (C) — filesystem write benchmark.
 *
 * Behaviour matches the Go implementation (scratch-dna.go):
 *   - same short/long options: -p/--parallel, -v/--verbose, -t/--timeout, -s/--sync
 *   - same positional args:    <number-of-files> <file-size-bytes>
 *                              <random-file-size-multiplier> <writable-directory>
 *   - fixed pool of <parallel> worker threads pulling file indices from a shared
 *     atomic counter (bounded memory regardless of file count)
 *   - per-worker RNG to avoid contention
 *   - each file is the shared DNA buffer written rand(1..multiplier) times
 *   - identical console output, --verbose progress, and timeout handling
 *
 * Build: gcc -O2 -pthread -o scratch-dna scratch-dna.c
 */
#define _GNU_SOURCE

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <getopt.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <fcntl.h>
#include <time.h>
#include <limits.h>
#include <sys/stat.h>
#include <sys/types.h>

#define GIB 1073741824.0
#define MIB 1048576.0

/* Directory fan-out limits, so no single folder holds an unwieldy number of
 * entries: each leaf subdirectory holds up to FILES_PER_DIR files, and each
 * parent directory holds up to SUBDIRS_PER_DIR leaf subdirectories. */
#define FILES_PER_DIR   10000
#define SUBDIRS_PER_DIR 100

/* In --unique mode a uniqueness tag is written every STAMP_STRIDE bytes: one
 * 8-byte tag per block. 4 KiB matches common dedup/compression block sizes, so
 * every such block differs across files. */
#define STAMP_STRIDE 4096

/* ----- timing -------------------------------------------------------------- */

static double now_sec(void)
{
   struct timespec ts;
   clock_gettime(CLOCK_MONOTONIC, &ts);
   return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

/* ----- RNG (xorshift128+ with splitmix64 seeding) -------------------------- */

static uint64_t splitmix64(uint64_t *x)
{
   uint64_t z = (*x += 0x9E3779B97F4A7C15ULL);
   z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
   z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
   return z ^ (z >> 31);
}

static uint64_t xrng(uint64_t s[2])
{
   uint64_t x = s[0];
   const uint64_t y = s[1];
   s[0] = y;
   x ^= x << 23;
   s[1] = x ^ y ^ (x >> 17) ^ (y >> 26);
   return s[1] + y;
}

static void seed_rng(uint64_t s[2], uint64_t seed)
{
   s[0] = splitmix64(&seed);
   s[1] = splitmix64(&seed);
   if (s[0] == 0 && s[1] == 0)
      s[0] = 1; /* xorshift must not be all-zero */
}

/* ----- directory bucketing ------------------------------------------------- */

/* bucket_for maps a 1-based file index to its leaf-bucket number: files
 * 1..FILES_PER_DIR go in bucket 0, the next FILES_PER_DIR in bucket 1, etc. */
static long long bucket_for(long long idx)
{
   return (idx - 1) / FILES_PER_DIR;
}

/* rand_name writes a random 16-hex-character directory name (NUL-terminated)
 * into out, which must hold at least 17 bytes. */
static void rand_name(char out[17], uint64_t s[2])
{
   static const char hex[] = "0123456789abcdef";
   uint64_t r = xrng(s);
   for (int i = 0; i < 16; i++)
      {
      out[i] = hex[r & 0xf];
      r >>= 4;
      if (i == 7)
         r = xrng(s);
      }
   out[16] = '\0';
}

/* plan_buckets creates, single-threaded and up front, every leaf directory
 * needed to hold n_files files under base, returning them indexed by bucket
 * number. Layout is base/<random-parent>/<random-sub>; a new random parent is
 * started after every SUBDIRS_PER_DIR leaf subdirectories. Creating all bucket
 * directories before any worker starts means workers never race on mkdir.
 * The caller frees the returned array and each string. */
static char **plan_buckets(const char *base, long long n_files, long long *n_buckets_out, uint64_t seed)
{
   if (mkdir(base, 0755) != 0 && errno != EEXIST)
      {
      fprintf(stderr, "Failed to create output directory %s: %s\n", base, strerror(errno));
      exit(1);
      }

   long long n_buckets = (n_files + FILES_PER_DIR - 1) / FILES_PER_DIR;
   if (n_buckets < 1)
      n_buckets = 1;

   char **dirs = (char **)malloc(sizeof(char *) * (size_t)n_buckets);
   if (dirs == NULL)
      {
      fprintf(stderr, "Error: failed to allocate bucket table\n");
      exit(1);
      }

   uint64_t s[2];
   seed_rng(s, seed);

   char parent[PATH_MAX];
   parent[0] = '\0';
   char name[17];

   for (long long b = 0; b < n_buckets; b++)
      {
      if (b % SUBDIRS_PER_DIR == 0)
         {
         rand_name(name, s);
         if (snprintf(parent, sizeof(parent), "%s/%s", base, name) >= (int)sizeof(parent))
            {
            fprintf(stderr, "Path too long under %s\n", base);
            exit(1);
            }
         if (mkdir(parent, 0755) != 0 && errno != EEXIST)
            {
            fprintf(stderr, "Failed to create directory %s: %s\n", parent, strerror(errno));
            exit(1);
            }
         }

      rand_name(name, s);
      char leaf[PATH_MAX];
      if (snprintf(leaf, sizeof(leaf), "%s/%s", parent, name) >= (int)sizeof(leaf))
         {
         fprintf(stderr, "Path too long under %s\n", parent);
         exit(1);
         }
      if (mkdir(leaf, 0755) != 0 && errno != EEXIST)
         {
         fprintf(stderr, "Failed to create directory %s: %s\n", leaf, strerror(errno));
         exit(1);
         }

      dirs[b] = strdup(leaf);
      if (dirs[b] == NULL)
         {
         fprintf(stderr, "Error: out of memory building bucket table\n");
         exit(1);
         }
      }

   *n_buckets_out = n_buckets;
   return dirs;
}

/* ----- shared state -------------------------------------------------------- */

typedef struct {
   const unsigned char *dna;
   int            size;       /* bytes per DNA buffer */
   long long      total;      /* number of files to write */
   int            mult;       /* random size multiplier */
   char         **buckets;    /* leaf directory per bucket, indexed by bucket # */
   const char    *hostname;
   int            do_sync;
   int            unique;     /* stamp each block to defeat dedup/cache */

   _Atomic long long next;    /* next file index to claim (1-based) */
   _Atomic long long nfiles;
   _Atomic long long nbytes;
   _Atomic long long errcount;
   atomic_int     stop;
   atomic_int     running;    /* live worker count */
} shared_t;

typedef struct {
   shared_t      *sh;
   uint64_t       s[2];
   unsigned char *buf;        /* buffer to write: shared DNA, or a private copy
                               * stamped in place when --unique is set */
} worker_t;

/* ----- I/O helpers --------------------------------------------------------- */

static int write_all(int fd, const unsigned char *buf, size_t n)
{
   size_t off = 0;
   while (off < n)
      {
      ssize_t w = write(fd, buf + off, n - off);
      if (w < 0)
         {
         if (errno == EINTR)
            continue;
         return -1;
         }
      off += (size_t)w;
      }
   return 0;
}

/* stamp writes an 8-byte uniqueness tag every STAMP_STRIDE bytes of buf. The
 * tag mixes the file index, the repetition (block) number, and the byte offset,
 * so every STAMP_STRIDE-aligned block is distinct across the whole run while
 * only a few bytes per block are touched. buf must be privately owned. */
static void stamp(unsigned char *buf, size_t n, long long file_idx, int block)
{
   uint64_t base = ((uint64_t)file_idx << 20) ^ ((uint64_t)block << 4);
   for (size_t off = 0; off + sizeof(uint64_t) <= n; off += STAMP_STRIDE)
      {
      uint64_t tag = base ^ (uint64_t)off;
      memcpy(buf + off, &tag, sizeof(tag));
      }
}

static void *worker(void *arg)
{
   worker_t *w  = (worker_t *)arg;
   shared_t *sh = w->sh;
   char path[PATH_MAX];

   for (;;)
      {
      if (atomic_load(&sh->stop))
         break;

      long long idx = atomic_fetch_add(&sh->next, 1);
      if (idx > sh->total)
         break;

      int multiple = (int)(xrng(w->s) % (uint64_t)sh->mult) + 1; /* [1, mult] */

      snprintf(path, sizeof(path), "%s/%s-%lld-%d.txt",
               sh->buckets[bucket_for(idx)], sh->hostname, idx, multiple);

      int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
      if (fd < 0)
         {
         fprintf(stderr, "Error: create %s: %s\n", path, strerror(errno));
         atomic_fetch_add(&sh->errcount, 1);
         continue;
         }

      long long written = 0;
      int failed = 0;
      for (int j = 0; j < multiple; j++)
         {
         if (sh->unique)
            stamp(w->buf, (size_t)sh->size, idx, j);
         if (write_all(fd, w->buf, (size_t)sh->size) < 0)
            {
            fprintf(stderr, "Error: write %s (iteration %d/%d): %s\n",
                    path, j + 1, multiple, strerror(errno));
            failed = 1;
            break;
            }
         written += sh->size;
         }

      if (!failed && sh->do_sync && fsync(fd) < 0)
         {
         fprintf(stderr, "Error: sync %s: %s\n", path, strerror(errno));
         failed = 1;
         }

      if (close(fd) < 0 && !failed)
         {
         fprintf(stderr, "Error: close %s: %s\n", path, strerror(errno));
         failed = 1;
         }

      if (failed)
         {
         atomic_fetch_add(&sh->errcount, 1);
         continue;
         }

      atomic_fetch_add(&sh->nfiles, 1);
      atomic_fetch_add(&sh->nbytes, written);
      }

   atomic_fetch_sub(&sh->running, 1);
   return NULL;
}

/* ----- DNA generation ------------------------------------------------------ */

static unsigned char *genstring(int size, uint64_t seed)
{
   fprintf(stderr, "Building random DNA sequence of %.1f MB...\n\n", (double)size / MIB);

   const unsigned char bases[4] = { 'G', 'A', 'T', 'C' };
   unsigned char *dna = (unsigned char *)malloc((size_t)size);
   if (dna == NULL)
      {
      fprintf(stderr, "Error: failed to allocate DNA buffer of %d bytes\n", size);
      exit(1);
      }

   uint64_t s[2];
   seed_rng(s, seed);

   uint64_t r = 0;
   int bits = 0;
   for (int i = 0; i < size; i++)
      {
      if (bits == 0)
         {
         r = xrng(s);
         bits = 64;
         }
      dna[i] = bases[r & 3]; /* 2 bits per base */
      r >>= 2;
      bits -= 2;
      }
   return dna;
}

/* ----- output -------------------------------------------------------------- *
 * All human-readable output goes to stderr; stdout is reserved for the final
 * JSON result object (see print_json_result).                                  */

static void print_progress(long long nfiles, long long nbytes,
                           double elapsed, long long total)
{
   if (elapsed <= 0)
      elapsed = 1e-9;
   double fps = (double)nfiles / elapsed;
   double tp  = (double)nbytes / elapsed;
   long long remaining = total - nfiles;
   double pct = (double)nfiles / (double)total * 100.0;
   fprintf(stderr, "Progress: %6.2f%% | Files: %7lld/%7lld | Data: %5.1f GiB | "
          "FPS: %7.0f | Throughput: %6.0f MiB/s | Remaining: %7lld\n",
          pct, nfiles, total, (double)nbytes / GIB, fps, tp / MIB, remaining);
}

static void print_disk_usage(long long nfiles, long long nbytes, double elapsed)
{
   if (elapsed <= 0)
      elapsed = 1e-9;
   double fps = (double)nfiles / elapsed;
   double tp  = (double)nbytes / elapsed;

   char sep[81];
   memset(sep, '=', 80);
   sep[80] = '\0';

   fprintf(stderr, "\n%s\n", sep);
   fprintf(stderr, "Benchmark Complete!\n");
   fprintf(stderr, "%s\n", sep);
   fprintf(stderr, "Files Written:    %lld\n", nfiles);
   fprintf(stderr, "Total Size:       %.2f GiB (%.2f MB)\n", (double)nbytes / GIB, (double)nbytes / MIB);
   fprintf(stderr, "Elapsed Time:     %.3f seconds\n", elapsed);
   fprintf(stderr, "Avg FPS:          %.0f files/second\n", fps);
   fprintf(stderr, "Avg Throughput:   %.0f MiB/s\n", tp / MIB);
   fprintf(stderr, "%s\n", sep);
}

/* print_json_result writes the final stats to stdout as a single JSON object.
 * stdout carries only this object, so callers can pipe it straight to a parser. */
static void print_json_result(long long total, long long nfiles, long long nbytes,
                              double elapsed, long long errs, int timed_out)
{
   if (elapsed <= 0)
      elapsed = 1e-9;
   double fps = (double)nfiles / elapsed;
   double tp  = (double)nbytes / elapsed;

   printf("{\n");
   printf("  \"files_requested\": %lld,\n", total);
   printf("  \"files_written\": %lld,\n", nfiles);
   printf("  \"bytes_written\": %lld,\n", nbytes);
   printf("  \"total_mb\": %.6f,\n", (double)nbytes / MIB);
   printf("  \"total_gib\": %.9f,\n", (double)nbytes / GIB);
   printf("  \"elapsed_seconds\": %.9f,\n", elapsed);
   printf("  \"files_per_second\": %.6f,\n", fps);
   printf("  \"throughput_mib_per_second\": %.6f,\n", tp / MIB);
   printf("  \"errors\": %lld,\n", errs);
   printf("  \"timed_out\": %s\n", timed_out ? "true" : "false");
   printf("}\n");
}

static void usage(const char *prog)
{
   fprintf(stderr, "Error! 4 positional arguments required.\n");
   fprintf(stderr, "\nUsage: %s [options] <number-of-files> <file-size-bytes> "
                   "<random-file-size-multiplier> <writable-directory>\n", prog);
   fprintf(stderr, "\nOptions (short and long forms are equivalent):\n");
   fprintf(stderr, "  -p, --parallel <n>   number of concurrent file writers (default 1)\n");
   fprintf(stderr, "  -v, --verbose        print progress every second\n");
   fprintf(stderr, "  -t, --timeout <sec>  timeout for the whole operation (default 600)\n");
   fprintf(stderr, "  -s, --sync           fsync() after each file write\n");
   fprintf(stderr, "  -u, --unique         stamp a unique tag every 4 KiB so no two blocks repeat\n");
   fprintf(stderr, "\nExample: %s -v -p 8 1024 10485760 1 /tmp\n", prog);
   fprintf(stderr, "         %s --verbose --parallel 8 1024 10485760 1 /tmp\n\n", prog);
}

/* ----- argument parsing ---------------------------------------------------- */

static long long must_positive(const char *s, const char *name)
{
   char *end;
   errno = 0;
   long long v = strtoll(s, &end, 10);
   if (errno != 0 || end == s || *end != '\0' || v < 1)
      {
      fprintf(stderr, "Invalid %s: \"%s\" (must be a positive integer)\n", name, s);
      exit(1);
      }
   return v;
}

int main(int argc, char **argv)
{
   const char *prog = argv[0];

   int parallel = 1;
   int verbose  = 0;
   int timeout  = 600;
   int do_sync  = 0;
   int unique   = 0;

   static struct option longopts[] = {
      { "parallel", required_argument, 0, 'p' },
      { "verbose",  no_argument,       0, 'v' },
      { "timeout",  required_argument, 0, 't' },
      { "sync",     no_argument,       0, 's' },
      { "unique",   no_argument,       0, 'u' },
      { "help",     no_argument,       0, 'h' },
      { 0, 0, 0, 0 }
   };

   int opt;
   while ((opt = getopt_long(argc, argv, "p:vt:suh", longopts, NULL)) != -1)
      {
      switch (opt)
         {
         case 'p': parallel = (int)must_positive(optarg, "parallel"); break;
         case 'v': verbose  = 1; break;
         case 't': timeout  = (int)must_positive(optarg, "timeout"); break;
         case 's': do_sync  = 1; break;
         case 'u': unique   = 1; break;
         case 'h':
         default:
            usage(prog);
            exit(opt == 'h' ? 0 : 1);
         }
      }

   if (argc - optind != 4)
      {
      usage(prog);
      exit(1);
      }

   long long nFlag = must_positive(argv[optind + 0], "number-of-files");
   int       sFlag = (int)must_positive(argv[optind + 1], "file-size-bytes");
   int       mFlag = (int)must_positive(argv[optind + 2], "random-file-size-multiplier");
   const char *dFlag = argv[optind + 3];

   struct stat st;
   if (stat(dFlag, &st) != 0)
      {
      fprintf(stderr, "Directory error: %s\n", strerror(errno));
      exit(1);
      }
   if (!S_ISDIR(st.st_mode))
      {
      fprintf(stderr, "Path is not a directory: %s\n", dFlag);
      exit(1);
      }

   /* Seed the one-off RNG used for DNA generation and the hostname suffix. */
   uint64_t base_seed = (uint64_t)time(NULL) ^ ((uint64_t)getpid() << 32);

   unsigned char *dna = genstring(sFlag, base_seed);

   /* Use this host's name as a subdirectory under the target path so that
    * multiple hosts writing to a shared filesystem do not step on each other. */
   char hostname[HOST_NAME_MAX + 1];
   if (gethostname(hostname, sizeof(hostname)) != 0)
      {
      fprintf(stderr, "Warning: could not get hostname: %s, using 'dna'\n", strerror(errno));
      strcpy(hostname, "dna");
      }
   hostname[HOST_NAME_MAX] = '\0';

   /* Plan the per-host directory layout up front so no single folder holds too
    * many entries: dFlag/hostname/<random-parent>/<random-sub>. All bucket
    * directories are created single-threaded here; workers just look them up. */
   char outDir[PATH_MAX];
   snprintf(outDir, sizeof(outDir), "%s/%s", dFlag, hostname);
   long long nbuckets = 0;
   char **buckets = plan_buckets(outDir, nFlag, &nbuckets, base_seed);

   fprintf(stderr, "Writing %lld files with filesizes between %.1f MB and %.1f MB...\n",
          nFlag, (double)sFlag / MIB, (double)sFlag / MIB * (double)mFlag);
   fprintf(stderr, "Using %d parallel workers\n\n", parallel);

   shared_t sh;
   sh.dna      = dna;
   sh.size     = sFlag;
   sh.total    = nFlag;
   sh.mult     = mFlag;
   sh.buckets  = buckets;
   sh.hostname = hostname;
   sh.do_sync  = do_sync;
   sh.unique   = unique;
   atomic_init(&sh.next, 1);
   atomic_init(&sh.nfiles, 0);
   atomic_init(&sh.nbytes, 0);
   atomic_init(&sh.errcount, 0);
   atomic_init(&sh.stop, 0);
   atomic_init(&sh.running, parallel);

   double start = now_sec();

   pthread_t *threads = (pthread_t *)malloc(sizeof(pthread_t) * parallel);
   worker_t  *workers = (worker_t *)malloc(sizeof(worker_t) * parallel);
   if (threads == NULL || workers == NULL)
      {
      fprintf(stderr, "Error: failed to allocate worker pool\n");
      exit(1);
      }

   for (int i = 0; i < parallel; i++)
      {
      workers[i].sh = &sh;
      seed_rng(workers[i].s, base_seed + (uint64_t)(i + 1) * 0x9E3779B97F4A7C15ULL);

      /* In --unique mode each worker stamps its buffer in place, so it needs a
       * private writable copy; otherwise all workers share the read-only DNA. */
      if (unique)
         {
         workers[i].buf = (unsigned char *)malloc((size_t)sFlag);
         if (workers[i].buf == NULL)
            {
            fprintf(stderr, "Error: failed to allocate per-worker buffer\n");
            exit(1);
            }
         memcpy(workers[i].buf, dna, (size_t)sFlag);
         }
      else
         workers[i].buf = dna;

      if (pthread_create(&threads[i], NULL, worker, &workers[i]) != 0)
         {
         fprintf(stderr, "Error: failed to create worker thread %d\n", i);
         atomic_fetch_sub(&sh.running, 1);
         }
      }

   /* Main loop: poll finely so short runs finish fast, but tick progress
    * roughly once a second and enforce the timeout. */
   double last_print = 0.0;
   struct timespec poll = { 0, 50 * 1000 * 1000 }; /* 50 ms */
   while (atomic_load(&sh.running) > 0)
      {
      nanosleep(&poll, NULL);
      double elapsed = now_sec() - start;

      if (elapsed >= (double)timeout)
         {
         atomic_store(&sh.stop, 1);
         fprintf(stderr, "ERROR: operation timed out after %d seconds\n", timeout);
         fprintf(stderr, "Files completed: %lld/%lld\n", atomic_load(&sh.nfiles), nFlag);
         print_disk_usage(atomic_load(&sh.nfiles), atomic_load(&sh.nbytes), elapsed);
         print_json_result(nFlag, atomic_load(&sh.nfiles), atomic_load(&sh.nbytes),
                           elapsed, atomic_load(&sh.errcount), 1);
         exit(1);
         }

      if (verbose && elapsed - last_print >= 1.0)
         {
         print_progress(atomic_load(&sh.nfiles), atomic_load(&sh.nbytes), elapsed, nFlag);
         last_print = elapsed;
         }
      }

   for (int i = 0; i < parallel; i++)
      pthread_join(threads[i], NULL);

   double elapsed = now_sec() - start;
   print_disk_usage(atomic_load(&sh.nfiles), atomic_load(&sh.nbytes), elapsed);

   long long errs = atomic_load(&sh.errcount);
   if (errs > 0)
      fprintf(stderr, "WARNING: completed with %lld errors\n", errs);

   print_json_result(nFlag, atomic_load(&sh.nfiles), atomic_load(&sh.nbytes),
                     elapsed, errs, 0);

   if (unique)
      for (int i = 0; i < parallel; i++)
         free(workers[i].buf);
   free(threads);
   free(workers);
   free(dna);
   for (long long b = 0; b < nbuckets; b++)
      free(buckets[b]);
   free(buckets);

   return errs > 0 ? 1 : 0;
}
