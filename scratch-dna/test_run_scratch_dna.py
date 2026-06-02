#!/usr/bin/env python3
"""Unit and end-to-end tests for run-scratch-dna.py.

Standard library only (unittest); no pytest, no third-party packages.

Covers:
  * parse_remote()    — local vs. remote target classification (unit)
  * write_svg_graph() — well-formed XML, empty-records handling (unit)
  * end-to-end        — build the C binary, run the orchestrator with
                        -p 2 -j 4 against a temp dir, assert the aggregated
                        JSON and the inst-NNNN subfolders.

Run with:  python3 -m unittest test_run_scratch_dna -v
       or:  python3 test_run_scratch_dna.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.dom.minidom as minidom

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(HERE, "run-scratch-dna.py")
C_SRC = os.path.join(HERE, "scratch-dna.c")


def load_module():
    """Import run-scratch-dna.py (dashes in name) via importlib."""
    spec = importlib.util.spec_from_file_location("run_scratch_dna", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rsd = load_module()


class ParseRemoteTests(unittest.TestCase):
    """parse_remote(spec) -> (user, host, path) for remote, None for local."""

    def test_user_host_path(self):
        self.assertEqual(
            rsd.parse_remote("alice@server:/data/bench"),
            ("alice", "server", "/data/bench"),
        )

    def test_host_path_no_user(self):
        self.assertEqual(
            rsd.parse_remote("server:/data/bench"),
            (None, "server", "/data/bench"),
        )

    def test_host_path_with_dots_and_fqdn(self):
        self.assertEqual(
            rsd.parse_remote("bob@host.example.com:/srv/x"),
            ("bob", "host.example.com", "/srv/x"),
        )

    def test_local_absolute_path(self):
        self.assertIsNone(rsd.parse_remote("/tmp/bench"))

    def test_local_relative_dot(self):
        self.assertIsNone(rsd.parse_remote("./bench"))

    def test_local_home(self):
        self.assertIsNone(rsd.parse_remote("~/bench"))

    def test_local_bare_relative(self):
        # A relative path with no colon is local.
        self.assertIsNone(rsd.parse_remote("bench/sub"))

    def test_relative_path_after_colon_is_not_remote(self):
        # Remote spec requires an ABSOLUTE path after the colon; a relative
        # path (no leading slash) must not match.
        self.assertIsNone(rsd.parse_remote("host:relative/path"))

    def test_plain_local_dir_name(self):
        self.assertIsNone(rsd.parse_remote("mydir"))


class WriteSvgGraphTests(unittest.TestCase):
    """write_svg_graph(path, records, title) -> writes well-formed SVG XML."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="svg-test-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _records(self):
        return [
            {"label": "inst-0000", "throughput_mib_per_second": 120.5,
             "files_written": 100, "host": "hostA"},
            {"label": "inst-0001", "throughput_mib_per_second": 80.0,
             "files_written": 90, "host": "hostB"},
            {"label": "inst-0002", "throughput_mib_per_second": 0.0,
             "files_written": 0, "host": "hostA"},
        ]

    def test_writes_well_formed_xml(self):
        out = os.path.join(self.tmp, "graph.svg")
        ok = rsd.write_svg_graph(out, self._records(), "test & <title>")
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(out))
        # Must parse as XML (validates well-formedness, including escaping).
        doc = minidom.parse(out)
        self.assertEqual(doc.documentElement.tagName, "svg")
        # Should contain one <rect> bar per record plus a background rect.
        rects = doc.getElementsByTagName("rect")
        self.assertGreaterEqual(len(rects), len(self._records()))

    def test_title_is_escaped(self):
        out = os.path.join(self.tmp, "graph2.svg")
        rsd.write_svg_graph(out, self._records(), "a & b < c > d")
        with open(out) as fh:
            content = fh.read()
        # Raw special chars must have been escaped in the output.
        self.assertIn("a &amp; b &lt; c &gt; d", content)
        # And it must still parse.
        minidom.parseString(content)

    def test_empty_records_returns_false(self):
        out = os.path.join(self.tmp, "empty.svg")
        ok = rsd.write_svg_graph(out, [], "nothing")
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(out))

    def test_multi_host_legend(self):
        out = os.path.join(self.tmp, "legend.svg")
        rsd.write_svg_graph(out, self._records(), "multi-host")
        doc = minidom.parse(out)
        # Two distinct hosts -> legend text nodes should mention both.
        text = doc.toxml()
        self.assertIn("hostA", text)
        self.assertIn("hostB", text)


class ParseArgsTests(unittest.TestCase):
    """parse_args validation (pure, no subprocess)."""

    def test_defaults(self):
        a = rsd.parse_args(["10", "1024", "1", "/tmp"])
        self.assertEqual(a.parallel, 1)
        self.assertEqual(a.threads, 4)
        self.assertIsNone(a.jobs)

    def test_rejects_non_positive(self):
        with self.assertRaises(SystemExit):
            rsd.parse_args(["0", "1024", "1", "/tmp"])

    def test_rejects_bad_disk_limit(self):
        with self.assertRaises(SystemExit):
            rsd.parse_args(["10", "1024", "1", "/tmp", "--disk-limit", "150"])


class EndToEndTests(unittest.TestCase):
    """Build the C binary and run the orchestrator end-to-end."""

    @classmethod
    def setUpClass(cls):
        cls.builddir = tempfile.mkdtemp(prefix="rsd-e2e-")
        cls.binary = os.path.join(cls.builddir, "scratch-dna-c")
        proc = subprocess.run(
            ["gcc", "-O2", "-pthread", "-o", cls.binary, C_SRC],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise unittest.SkipTest(
                "could not build scratch-dna.c: %s" % proc.stderr.decode())

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.builddir, ignore_errors=True)

    def test_orchestrator_p2_j4(self):
        root = tempfile.mkdtemp(prefix="rsd-root-")
        try:
            cmd = [
                sys.executable, MODULE_PATH,
                "-p", "2", "-j", "4", "--bin", self.binary, "--no-graph",
                "3", "1024", "1", root,
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            self.assertEqual(proc.returncode, 0,
                             msg="stderr:\n" + proc.stderr.decode())

            # stdout must be a single JSON object.
            summary = json.loads(proc.stdout.decode())

            expected_keys = {
                "instances_launched", "instances_completed", "instances_failed",
                "instances_timed_out", "files_written", "bytes_written",
                "total_mb", "total_gib", "elapsed_seconds", "files_per_second",
                "throughput_mib_per_second", "errors", "disk_used_fraction",
                "processes", "threads_per_process", "keep_going", "host",
                "per_instance",
            }
            self.assertEqual(set(summary.keys()), expected_keys)

            self.assertEqual(summary["instances_launched"], 4)
            self.assertEqual(summary["instances_completed"], 4)
            self.assertEqual(summary["instances_failed"], 0)
            self.assertEqual(summary["instances_timed_out"], 0)
            self.assertEqual(summary["errors"], 0)
            self.assertEqual(summary["processes"], 2)
            self.assertEqual(summary["threads_per_process"], 4)
            # 4 instances x 3 files each.
            self.assertEqual(summary["files_written"], 12)
            self.assertEqual(len(summary["per_instance"]), 4)

            # inst-NNNN subfolders exist, one per instance.
            subdirs = sorted(d for d in os.listdir(root)
                             if d.startswith("inst-"))
            self.assertEqual(subdirs,
                             ["inst-0000", "inst-0001", "inst-0002", "inst-0003"])

            # On-disk file count matches files_written.
            count = 0
            for dirpath, _, files in os.walk(root):
                count += sum(1 for f in files if f.endswith(".txt"))
            self.assertEqual(count, summary["files_written"])
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_orchestrator_writes_graph(self):
        root = tempfile.mkdtemp(prefix="rsd-graph-")
        try:
            cmd = [
                sys.executable, MODULE_PATH,
                "-p", "2", "-j", "2", "--bin", self.binary,
                "2", "1024", "1", root,
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            self.assertEqual(proc.returncode, 0,
                             msg="stderr:\n" + proc.stderr.decode())
            graph = os.path.join(root, "scratch-dna-results.svg")
            self.assertTrue(os.path.isfile(graph), "expected default SVG graph")
            minidom.parse(graph)  # must be well-formed XML
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
