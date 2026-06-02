#!/usr/bin/env python3
"""run-scratch-dna.py — orchestrate parallel scratch-dna-c processes.

This driver launches the compiled C benchmark (scratch-dna-c) as multiple
concurrent OS processes, fed through a bounded queue. The positional arguments
mirror the Go/C versions:

    <number-of-files> <file-size-bytes> <random-file-size-multiplier> <root-dir>

Concurrency is two-dimensional:

  * -p/--parallel N   how many C *processes* run at once (the queue's width)
  * -T/--threads  M   how many writer *threads* each C process uses (forwarded
                      as the C binary's own -p)

So `-p 4 -T 4` runs 4 processes, each writing with 4 threads (16-way total).

Each C instance writes into its own subfolder under <root-dir> (root/inst-NNNN/),
so the instances never collide; the C binary then creates its usual
<hostname>/... tree beneath that.

Remote targets: if <root-dir> looks like "[user@]host:/path", the binaries and
this script are copied to ~/bin on that server and the benchmark is executed
there over SSH; the JSON results are captured back and merged locally.

Output contract matches the Go/C versions: stdout carries ONLY a final JSON
summary (aggregated across all instances); all human-readable text goes to
stderr. An SVG performance graph is written next to the results (--graph).
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# [user@]host:/abs/path  — host must contain no slash; path must be absolute.
REMOTE_RE = re.compile(r"^(?:(?P<user>[^@/\s]+)@)?(?P<host>[^@/:\s]+):(?P<path>/.*)$")


def eprint(*args, **kwargs):
    """Print to stderr (human-readable channel)."""
    print(*args, file=sys.stderr, **kwargs)
    sys.stderr.flush()


def parse_remote(spec):
    """Return (user_or_None, host, path) if spec is a remote target, else None.

    A leading '/', '.', or '~' marks a local path and is never treated as
    remote (avoids mis-parsing absolute paths that contain a colon).
    """
    if spec[:1] in ("/", ".", "~"):
        return None
    m = REMOTE_RE.match(spec)
    if not m:
        return None
    return (m.group("user"), m.group("host"), m.group("path"))


def find_binary(explicit):
    """Locate the scratch-dna-c binary.

    Order: --bin, $SCRATCH_DNA_C, PATH, alongside this script.
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("SCRATCH_DNA_C")
    if env:
        candidates.append(env)
    on_path = shutil.which("scratch-dna-c")
    if on_path:
        candidates.append(on_path)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "scratch-dna-c"))

    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def disk_used_fraction(path):
    """Return fraction (0..1) of the filesystem holding `path` that is used."""
    usage = shutil.disk_usage(path)
    return (usage.total - usage.free) / usage.total


def build_cmd(binary, args, subdir):
    """Construct the argv for one C instance."""
    cmd = [binary, "-p", str(args.threads), "-t", str(args.timeout)]
    if args.sync:
        cmd.append("-s")
    if args.unique:
        cmd.append("-u")
    cmd += [str(args.num_files), str(args.file_size), str(args.multiplier), subdir]
    return cmd


def run_instance(binary, args, root, index, host_label, verbose):
    """Run a single C instance into its own subfolder; return its parsed JSON."""
    subdir = os.path.join(root, "inst-%04d" % index)
    os.makedirs(subdir, exist_ok=True)
    cmd = build_cmd(binary, args, subdir)

    if verbose:
        eprint("[inst %04d] launching: %s" % (index, " ".join(cmd)))

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        eprint("[inst %04d] failed to launch: %s" % (index, e))
        return {"files_written": 0, "bytes_written": 0, "errors": 1,
                "timed_out": False, "launch_failed": True,
                "instance": index, "host": host_label}

    if verbose and proc.stderr:
        for line in proc.stderr.decode(errors="replace").splitlines():
            eprint("[inst %04d] %s" % (index, line))

    try:
        result = json.loads(proc.stdout.decode(errors="replace"))
    except (json.JSONDecodeError, ValueError):
        eprint("[inst %04d] could not parse JSON output (exit %d)"
               % (index, proc.returncode))
        return {"files_written": 0, "bytes_written": 0, "errors": 1,
                "timed_out": False, "parse_failed": True,
                "instance": index, "host": host_label}

    result["instance"] = index
    result["exit_code"] = proc.returncode
    result["host"] = host_label
    if verbose:
        eprint("[inst %04d] done: %d files, %.1f MiB/s%s"
               % (index, result.get("files_written", 0),
                  result.get("throughput_mib_per_second", 0.0),
                  " (TIMED OUT)" if result.get("timed_out") else ""))
    return result


# --------------------------------------------------------------------------- #
# Remote execution                                                            #
# --------------------------------------------------------------------------- #

def run_remote(user, host, path, args, argv_flags):
    """Copy binaries + this script to ~/bin on the remote and run there.

    Returns the remote run's JSON summary (with a 'remote' marker), or a
    failure sentinel dict.
    """
    target = ("%s@%s" % (user, host)) if user else host
    here = os.path.dirname(os.path.abspath(__file__))
    binary = find_binary(args.bin)
    if binary is None:
        eprint("Error: no local scratch-dna-c binary to copy to %s" % target)
        return {"remote": target, "instances_failed": 1, "errors": 1,
                "files_written": 0, "bytes_written": 0}

    script = os.path.abspath(__file__)
    payload = [binary, script]
    go_bin = os.path.join(here, "scratch-dna-go")
    if os.path.isfile(go_bin):
        payload.append(go_bin)

    eprint("[remote %s] preparing ~/bin and copying %d file(s)..."
           % (target, len(payload)))

    # Ensure ~/bin exists.
    rc = subprocess.run(["ssh", target, "mkdir -p ~/bin"]).returncode
    if rc != 0:
        eprint("[remote %s] ssh mkdir failed (rc=%d)" % (target, rc))
        return {"remote": target, "instances_failed": 1, "errors": 1,
                "files_written": 0, "bytes_written": 0}

    # Copy payload, normalising the C binary / script to canonical names.
    copies = [
        (binary, "scratch-dna-c"),
        (script, "run-scratch-dna.py"),
    ]
    if os.path.isfile(go_bin):
        copies.append((go_bin, "scratch-dna-go"))
    for src, dst in copies:
        rc = subprocess.run(["scp", "-q", src, "%s:bin/%s" % (target, dst)]).returncode
        if rc != 0:
            eprint("[remote %s] scp of %s failed (rc=%d)" % (target, dst, rc))
            return {"remote": target, "instances_failed": 1, "errors": 1,
                    "files_written": 0, "bytes_written": 0}

    # Make them executable and run the script remotely against the remote path.
    # The remote path is local on the remote host, so there is no recursion.
    remote_cmd = (
        "chmod +x ~/bin/scratch-dna-c ~/bin/run-scratch-dna.py 2>/dev/null; "
        "SCRATCH_DNA_C=~/bin/scratch-dna-c python3 ~/bin/run-scratch-dna.py "
        "--no-graph %s %s" % (" ".join(argv_flags), shlex_quote(path))
    )
    eprint("[remote %s] running benchmark..." % target)
    proc = subprocess.run(["ssh", target, remote_cmd],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.stderr:
        for line in proc.stderr.decode(errors="replace").splitlines():
            eprint("[remote %s] %s" % (target, line))

    try:
        summary = json.loads(proc.stdout.decode(errors="replace"))
    except (json.JSONDecodeError, ValueError):
        eprint("[remote %s] could not parse remote JSON (exit %d)"
               % (target, proc.returncode))
        return {"remote": target, "instances_failed": 1, "errors": 1,
                "files_written": 0, "bytes_written": 0}

    summary["remote"] = target
    return summary


def shlex_quote(s):
    import shlex
    return shlex.quote(s)


# --------------------------------------------------------------------------- #
# SVG graph                                                                   #
# --------------------------------------------------------------------------- #

def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def write_svg_graph(path, records, title):
    """Render a horizontal bar chart of per-instance throughput as an SVG.

    `records` is a list of dicts with keys: label, throughput_mib_per_second,
    files_written, host. Pure-Python; no external dependencies.
    """
    if not records:
        return False

    records = sorted(records, key=lambda r: r.get("throughput_mib_per_second", 0.0),
                     reverse=True)

    # Layout constants.
    width = 900
    margin_l = 220
    margin_r = 110
    margin_t = 80
    margin_b = 50
    row_h = 30
    bar_h = 20
    plot_w = width - margin_l - margin_r
    height = margin_t + margin_b + row_h * len(records)

    max_tp = max((r.get("throughput_mib_per_second", 0.0) for r in records), default=0.0)
    if max_tp <= 0:
        max_tp = 1.0

    # Stable color per host.
    hosts = []
    for r in records:
        h = r.get("host", "local")
        if h not in hosts:
            hosts.append(h)
    palette = ["#4f8cff", "#34d399", "#fbbf24", "#f472b6", "#a78bfa",
               "#22d3ee", "#fb7185", "#84cc16"]
    host_color = {h: palette[i % len(palette)] for i, h in enumerate(hosts)}

    parts = []
    parts.append(
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" font-family="-apple-system,Segoe UI,Roboto,sans-serif">'
        % (width, height, width, height))

    # Background + subtle gradient defs.
    parts.append('<defs>')
    for h, c in host_color.items():
        cid = "g_" + re.sub(r"[^A-Za-z0-9]", "_", h)
        parts.append(
            '<linearGradient id="%s" x1="0" y1="0" x2="1" y2="0">'
            '<stop offset="0" stop-color="%s" stop-opacity="0.95"/>'
            '<stop offset="1" stop-color="%s" stop-opacity="0.65"/>'
            '</linearGradient>' % (cid, c, c))
    parts.append('</defs>')
    parts.append('<rect width="%d" height="%d" fill="#0f172a"/>' % (width, height))

    # Title.
    parts.append('<text x="%d" y="38" fill="#f8fafc" font-size="22" '
                 'font-weight="700">%s</text>' % (margin_l // 4, _esc(title)))
    parts.append('<text x="%d" y="60" fill="#94a3b8" font-size="13">'
                 'throughput per instance (MiB/s)</text>' % (margin_l // 4))

    # X gridlines (4 ticks).
    for t in range(5):
        gx = margin_l + plot_w * t / 4
        val = max_tp * t / 4
        parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="#1e293b" '
                     'stroke-width="1"/>' % (gx, margin_t - 10, gx, height - margin_b))
        parts.append('<text x="%.1f" y="%d" fill="#64748b" font-size="11" '
                     'text-anchor="middle">%.0f</text>'
                     % (gx, height - margin_b + 18, val))

    # Bars.
    for i, r in enumerate(records):
        y = margin_t + i * row_h
        tp = r.get("throughput_mib_per_second", 0.0)
        bw = plot_w * (tp / max_tp)
        h = r.get("host", "local")
        cid = "g_" + re.sub(r"[^A-Za-z0-9]", "_", h)
        label = r.get("label", "inst")

        parts.append('<text x="%d" y="%d" fill="#cbd5e1" font-size="12" '
                     'text-anchor="end">%s</text>'
                     % (margin_l - 10, y + bar_h - 4, _esc(label)))
        parts.append('<rect x="%d" y="%d" width="%.1f" height="%d" rx="4" '
                     'fill="url(#%s)"/>' % (margin_l, y, max(bw, 1), bar_h, cid))
        parts.append('<text x="%.1f" y="%d" fill="#e2e8f0" font-size="11">'
                     '%.0f MiB/s</text>' % (margin_l + bw + 6, y + bar_h - 5, tp))

    # Legend (only if more than one host).
    if len(hosts) > 1:
        lx = margin_l
        ly = height - 14
        for h in hosts:
            parts.append('<rect x="%d" y="%d" width="12" height="12" rx="2" '
                         'fill="%s"/>' % (lx, ly - 10, host_color[h]))
            parts.append('<text x="%d" y="%d" fill="#94a3b8" font-size="11">%s</text>'
                         % (lx + 18, ly, _esc(h)))
            lx += 30 + 8 * len(h)

    parts.append('</svg>')

    with open(path, "w") as fh:
        fh.write("\n".join(parts))
    return True


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="run-scratch-dna.py",
        description="Orchestrate parallel scratch-dna-c processes through a queue.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("num_files", type=int, help="number of files per C instance")
    p.add_argument("file_size", type=int, help="file size in bytes")
    p.add_argument("multiplier", type=int, help="random file-size multiplier")
    p.add_argument("root_dir",
                   help="writable root dir, or [user@]host:/path for a remote target")

    p.add_argument("-p", "--parallel", type=int, default=1,
                   help="number of C processes to run concurrently (queue width; default 1)")
    p.add_argument("-T", "--threads", type=int, default=4,
                   help="writer threads per C process, forwarded as the binary's -p (default 4)")
    p.add_argument("-j", "--jobs", type=int, default=None,
                   help="total number of C instances to run (default: same as --parallel; "
                        "ignored with --keep-going)")
    p.add_argument("-t", "--timeout", type=int, default=600,
                   help="per-instance timeout in seconds (default 600)")
    p.add_argument("-s", "--sync", action="store_true",
                   help="fsync() after each file write (forwarded)")
    p.add_argument("-u", "--unique", action="store_true",
                   help="stamp each block unique to defeat dedup/cache (forwarded)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print per-instance progress to stderr")
    p.add_argument("--keep-going", action="store_true",
                   help="loop forever, launching instances until the disk is full enough")
    p.add_argument("--disk-limit", type=float, default=80.0,
                   help="with --keep-going, stop once this %% of the disk is used (default 80)")
    p.add_argument("--graph", default=None,
                   help="path for the SVG performance graph (default: <root>/scratch-dna-results.svg)")
    p.add_argument("--no-graph", action="store_true",
                   help="do not write an SVG graph (used for remote sub-runs)")
    p.add_argument("--bin", default=None,
                   help="path to the scratch-dna-c binary (default: search PATH and script dir)")

    args = p.parse_args(argv)

    for name, val in (("num_files", args.num_files), ("file_size", args.file_size),
                      ("multiplier", args.multiplier), ("parallel", args.parallel),
                      ("threads", args.threads), ("timeout", args.timeout)):
        if val < 1:
            p.error("%s must be a positive integer (got %d)" % (name, val))
    if args.jobs is not None and args.jobs < 1:
        p.error("jobs must be a positive integer")
    if not (0 < args.disk_limit < 100):
        p.error("disk-limit must be between 0 and 100")
    return args


def forwardable_flags(args):
    """Reconstruct the flag list (no positionals) to forward to a remote run."""
    flags = ["-p", str(args.parallel), "-T", str(args.threads),
             "-t", str(args.timeout)]
    if args.jobs is not None:
        flags += ["-j", str(args.jobs)]
    if args.sync:
        flags.append("-s")
    if args.unique:
        flags.append("-u")
    if args.verbose:
        flags.append("-v")
    if args.keep_going:
        flags.append("--keep-going")
        flags += ["--disk-limit", str(args.disk_limit)]
    return flags


def run_local(args, host_label):
    """Run instances locally; return (results list, elapsed seconds)."""
    binary = find_binary(args.bin)
    if binary is None:
        eprint("Error: could not find the scratch-dna-c binary.")
        eprint("Build it with:  gcc -O2 -pthread -o scratch-dna-c scratch-dna.c")
        eprint("or pass --bin /path/to/scratch-dna-c")
        return None, 0.0

    eprint("Using binary: %s" % binary)
    eprint("Concurrency: %d processes x %d threads each" % (args.parallel, args.threads))
    if args.keep_going:
        eprint("Mode: --keep-going (until %.0f%% of disk used)" % args.disk_limit)
    else:
        total = args.jobs if args.jobs is not None else args.parallel
        eprint("Mode: %d total instance(s)" % total)
    eprint("")

    start = time.monotonic()
    results = []
    next_index = 0
    limit_frac = args.disk_limit / 100.0

    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        if args.keep_going:
            in_flight = set()
            stopping = False
            while True:
                while not stopping and len(in_flight) < args.parallel:
                    used = disk_used_fraction(args.root_dir)
                    if used >= limit_frac:
                        eprint("Disk at %.1f%% (limit %.0f%%) — no longer launching new instances."
                               % (used * 100, args.disk_limit))
                        stopping = True
                        break
                    fut = pool.submit(run_instance, binary, args, args.root_dir,
                                      next_index, host_label, args.verbose)
                    in_flight.add(fut)
                    next_index += 1
                if not in_flight:
                    break
                done = next(as_completed(in_flight))
                in_flight.discard(done)
                results.append(done.result())
        else:
            total = args.jobs if args.jobs is not None else args.parallel
            futures = [pool.submit(run_instance, binary, args, args.root_dir, i,
                                   host_label, args.verbose)
                       for i in range(total)]
            for fut in as_completed(futures):
                results.append(fut.result())

    elapsed = time.monotonic() - start
    return results, max(elapsed, 1e-9)


def aggregate(results):
    agg = {"files_written": 0, "bytes_written": 0, "errors": 0,
           "instances_completed": 0, "instances_timed_out": 0, "instances_failed": 0}
    for r in results:
        agg["files_written"] += r.get("files_written", 0)
        agg["bytes_written"] += r.get("bytes_written", 0)
        agg["errors"] += r.get("errors", 0)
        agg["instances_completed"] += 1
        if r.get("timed_out"):
            agg["instances_timed_out"] += 1
        if r.get("launch_failed") or r.get("parse_failed") or r.get("exit_code", 0) != 0:
            agg["instances_failed"] += 1
    return agg


def main(argv):
    args = parse_args(argv)
    remote = parse_remote(args.root_dir)

    # ----- Remote target: copy + execute over SSH, then graph locally. ------ #
    if remote is not None:
        user, host, path = remote
        summary = run_remote(user, host, path, args, forwardable_flags(args))
        target = summary.get("remote", host)

        # Pull per-instance records out of the remote summary for graphing.
        records = []
        for r in summary.get("per_instance", []):
            records.append({
                "label": "%s:inst-%04d" % (host, r.get("instance", 0)),
                "throughput_mib_per_second": r.get("throughput_mib_per_second", 0.0),
                "files_written": r.get("files_written", 0),
                "host": target,
            })

        eprint("\n%s" % ("=" * 80))
        eprint("Remote run on %s complete." % target)
        eprint("Files Written:    %d" % summary.get("files_written", 0))
        eprint("Total Size:       %.2f GiB" % (summary.get("bytes_written", 0) / 1073741824))
        eprint("Avg Throughput:   %.0f MiB/s" % summary.get("throughput_mib_per_second", 0.0))
        eprint("=" * 80)

        if not args.no_graph and records:
            graph_path = args.graph or "scratch-dna-results.svg"
            if write_svg_graph(graph_path, records, "scratch-dna @ %s" % target):
                eprint("Graph written: %s" % graph_path)

        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 1 if (summary.get("instances_failed", 0) or summary.get("errors", 0)) else 0

    # ----- Local run. ------------------------------------------------------- #
    if not os.path.isdir(args.root_dir):
        eprint("Error: root directory does not exist: %s" % args.root_dir)
        return 2

    host_label = socket.gethostname()
    results, elapsed = run_local(args, host_label)
    if results is None:
        return 2

    agg = aggregate(results)
    per_instance = [
        {"instance": r.get("instance", i),
         "host": r.get("host", host_label),
         "files_written": r.get("files_written", 0),
         "bytes_written": r.get("bytes_written", 0),
         "throughput_mib_per_second": r.get("throughput_mib_per_second", 0.0),
         "timed_out": r.get("timed_out", False)}
        for i, r in enumerate(results)
    ]
    summary = {
        "instances_launched": len(results),
        "instances_completed": agg["instances_completed"],
        "instances_failed": agg["instances_failed"],
        "instances_timed_out": agg["instances_timed_out"],
        "files_written": agg["files_written"],
        "bytes_written": agg["bytes_written"],
        "total_mb": agg["bytes_written"] / 1048576,
        "total_gib": agg["bytes_written"] / 1073741824,
        "elapsed_seconds": elapsed,
        "files_per_second": agg["files_written"] / elapsed,
        "throughput_mib_per_second": agg["bytes_written"] / elapsed / 1048576,
        "errors": agg["errors"],
        "disk_used_fraction": disk_used_fraction(args.root_dir),
        "processes": args.parallel,
        "threads_per_process": args.threads,
        "keep_going": args.keep_going,
        "host": host_label,
        "per_instance": per_instance,
    }

    sep = "=" * 80
    eprint("\n%s" % sep)
    eprint("Orchestration Complete!")
    eprint(sep)
    eprint("Instances:        %d launched, %d failed, %d timed out"
           % (summary["instances_launched"], summary["instances_failed"],
              summary["instances_timed_out"]))
    eprint("Files Written:    %d" % summary["files_written"])
    eprint("Total Size:       %.2f GiB (%.2f MB)" % (summary["total_gib"], summary["total_mb"]))
    eprint("Elapsed Time:     %.3f seconds" % summary["elapsed_seconds"])
    eprint("Avg Throughput:   %.0f MiB/s" % summary["throughput_mib_per_second"])
    eprint("Disk Used:        %.1f%%" % (summary["disk_used_fraction"] * 100))
    eprint(sep)

    if not args.no_graph:
        graph_path = args.graph or os.path.join(args.root_dir, "scratch-dna-results.svg")
        records = [
            {"label": "inst-%04d" % r["instance"],
             "throughput_mib_per_second": r["throughput_mib_per_second"],
             "files_written": r["files_written"],
             "host": r["host"]}
            for r in per_instance
        ]
        if write_svg_graph(graph_path, records, "scratch-dna @ %s" % host_label):
            eprint("Graph written: %s" % graph_path)

    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()

    return 1 if (summary["instances_failed"] > 0 or summary["errors"] > 0) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
