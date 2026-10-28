"""Tests for dupe_finder.

Each test builds a small throwaway folder, runs the finder on it, and checks
the answer. Reading these is a good way to see what the tool is supposed to do.

Run them from this folder with:   python -m unittest -v
"""

import argparse
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dupe_finder


class DupeFinderTests(unittest.TestCase):
    def setUp(self):
        # A fresh temporary folder for every test, deleted automatically after.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def write(self, relative_path: str, content: bytes = b"hello") -> Path:
        """Create a file (and any folders above it) inside the test folder."""
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def names_in(self, group) -> set:
        return {p.name for p in group.paths}

    # --- the core behavior --------------------------------------------------

    def test_identical_files_with_different_names_are_found(self):
        self.write("report.txt", b"same content")
        self.write("report copy.txt", b"same content")
        result = dupe_finder.find_duplicates(self.root)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(self.names_in(result.groups[0]), {"report.txt", "report copy.txt"})

    def test_same_size_but_different_content_is_not_a_duplicate(self):
        # Both are 5 bytes, so they land in the same size bucket, but the
        # fingerprint step should tell them apart.
        self.write("a.txt", b"apple")
        self.write("b.txt", b"grape")
        self.assertEqual(dupe_finder.find_duplicates(self.root).groups, [])

    def test_unique_files_are_not_reported(self):
        self.write("one.txt", b"1")
        self.write("two.txt", b"22")
        self.write("three.txt", b"333")
        self.assertEqual(dupe_finder.find_duplicates(self.root).groups, [])

    def test_duplicates_across_subfolders_are_found(self):
        self.write("photos/2024/cat.jpg", b"cat-bytes")
        self.write("backup/old/cat.jpg", b"cat-bytes")
        result = dupe_finder.find_duplicates(self.root)
        self.assertEqual(len(result.groups), 1)

    def test_three_copies_form_one_group(self):
        for name in ("a.bin", "b.bin", "c.bin"):
            self.write(name, b"xyz")
        result = dupe_finder.find_duplicates(self.root)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(len(result.groups[0].paths), 3)

    def test_wasted_bytes_counts_every_copy_but_one(self):
        for name in ("a.bin", "b.bin", "c.bin"):
            self.write(name, b"0123456789")  # 10 bytes each
        group = dupe_finder.find_duplicates(self.root).groups[0]
        self.assertEqual(group.wasted_bytes, 20)  # keep one, reclaim two

    def test_biggest_savings_are_listed_first(self):
        self.write("small1.bin", b"a" * 10)
        self.write("small2.bin", b"a" * 10)
        self.write("big1.bin", b"b" * 1000)
        self.write("big2.bin", b"b" * 1000)
        groups = dupe_finder.find_duplicates(self.root).groups
        self.assertEqual([g.size for g in groups], [1000, 10])

    # --- things that are skipped on purpose ---------------------------------

    def test_empty_files_are_ignored_by_default(self):
        self.write("empty1.txt", b"")
        self.write("empty2.txt", b"")
        self.assertEqual(dupe_finder.find_duplicates(self.root).groups, [])

    def test_empty_files_can_be_included(self):
        self.write("empty1.txt", b"")
        self.write("empty2.txt", b"")
        result = dupe_finder.find_duplicates(self.root, min_size=0)
        self.assertEqual(len(result.groups), 1)

    def test_min_size_filters_small_files(self):
        self.write("a.txt", b"tiny")
        self.write("b.txt", b"tiny")
        self.assertEqual(dupe_finder.find_duplicates(self.root, min_size=100).groups, [])

    def test_hidden_files_skipped_unless_requested(self):
        self.write(".secret", b"data")
        self.write("visible.txt", b"data")
        self.assertEqual(dupe_finder.find_duplicates(self.root).groups, [])
        result = dupe_finder.find_duplicates(self.root, include_hidden=True)
        self.assertEqual(len(result.groups), 1)

    def test_hidden_folders_skipped_unless_requested(self):
        self.write(".git/objects/blob", b"data")
        self.write("notes.txt", b"data")
        self.assertEqual(dupe_finder.find_duplicates(self.root).groups, [])
        self.assertEqual(len(dupe_finder.find_duplicates(self.root, include_hidden=True).groups), 1)

    def test_symlinks_are_ignored(self):
        original = self.write("original.txt", b"data")
        try:
            os.symlink(original, self.root / "shortcut.txt")
        except (OSError, NotImplementedError):
            self.skipTest("this system can't create symlinks")
        self.assertEqual(dupe_finder.find_duplicates(self.root).groups, [])

    def test_hard_links_are_not_counted_as_duplicates(self):
        original = self.write("original.txt", b"data")
        try:
            os.link(original, self.root / "second_name.txt")
        except (OSError, NotImplementedError):
            self.skipTest("this system can't create hard links")
        # Two names, one piece of data: deleting either frees no space.
        self.assertEqual(dupe_finder.find_duplicates(self.root).groups, [])

    # --- failure handling ---------------------------------------------------

    def test_unreadable_file_is_reported_not_fatal(self):
        good1 = self.write("good1.txt", b"same")
        self.write("good2.txt", b"same")
        bad = self.write("bad.txt", b"same")
        real_hash = dupe_finder.hash_file

        def flaky_hash(path):
            if path == bad:
                raise PermissionError(13, "Permission denied")
            return real_hash(path)

        with mock.patch.object(dupe_finder, "hash_file", flaky_hash):
            result = dupe_finder.find_duplicates(self.root)

        # The two readable files are still found...
        self.assertEqual(len(result.groups), 1)
        self.assertNotIn(bad, result.groups[0].paths)
        self.assertIn(good1, result.groups[0].paths)
        # ...and the problem is recorded rather than crashing the scan.
        self.assertEqual(len(result.problems), 1)
        self.assertIn("bad.txt", result.problems[0])

    # --- sizes --------------------------------------------------------------

    def test_human_size(self):
        self.assertEqual(dupe_finder.human_size(0), "0 B")
        self.assertEqual(dupe_finder.human_size(1023), "1023 B")
        self.assertEqual(dupe_finder.human_size(1536), "1.5 KB")
        self.assertEqual(dupe_finder.human_size(5 * 1024**2), "5.0 MB")

    def test_parse_size(self):
        self.assertEqual(dupe_finder.parse_size("100"), 100)
        self.assertEqual(dupe_finder.parse_size("10KB"), 10 * 1024)
        self.assertEqual(dupe_finder.parse_size("1.5 mb"), int(1.5 * 1024**2))
        self.assertEqual(dupe_finder.parse_size("2GB"), 2 * 1024**3)

    def test_parse_size_rejects_nonsense(self):
        for bad in ("big", "-5KB", "10XB", ""):
            with self.subTest(text=bad):
                with self.assertRaises(argparse.ArgumentTypeError):
                    dupe_finder.parse_size(bad)

    # --- the command line ---------------------------------------------------

    def run_cli(self, *args):
        """Run main() and capture what it prints."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dupe_finder.main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()

    def test_cli_reports_duplicates(self):
        self.write("a.txt", b"dup!")
        self.write("b.txt", b"dup!")
        code, out, _ = self.run_cli(self.root)
        self.assertEqual(code, 0)
        self.assertIn("1 set(s) of duplicates", out)
        self.assertIn("a.txt", out)
        self.assertIn("b.txt", out)

    def test_cli_says_so_when_nothing_found(self):
        self.write("only.txt", b"alone")
        code, out, _ = self.run_cli(self.root)
        self.assertEqual(code, 0)
        self.assertIn("No duplicate files found.", out)

    def test_cli_rejects_a_missing_folder(self):
        code, _, err = self.run_cli(self.root / "does-not-exist")
        self.assertEqual(code, 1)
        self.assertIn("Not a folder", err)

    def test_cli_never_deletes_anything(self):
        a = self.write("a.txt", b"dup!")
        b = self.write("b.txt", b"dup!")
        self.run_cli(self.root)
        self.assertTrue(a.exists() and b.exists())


if __name__ == "__main__":
    unittest.main()
