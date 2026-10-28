#!/usr/bin/env python3
"""
dupe_finder: find files that are exact copies of each other.

WHAT IT DOES
------------
Point it at a folder and it searches that folder, and every folder inside it,
for files with identical contents. File NAMES don't matter: "IMG_001.jpg" and
"vacation copy (2).jpg" count as duplicates if the bytes inside are the same.

It prints each set of duplicates, and how much disk space you would get back
by keeping just one copy of each.

IT NEVER DELETES ANYTHING. This tool only reports. Deciding which copy to keep
is a human decision, so it is left to you.

HOW IT WORKS (the short version)
--------------------------------
Comparing every file against every other file would be painfully slow, so the
search narrows things down from cheap checks to expensive ones:

  1. Group files by SIZE. Two files of different sizes cannot be identical, and
     asking the operating system for a file's size is nearly instant. Most
     files are ruled out here without ever being opened.

  2. For files that share a size, compute a FINGERPRINT of their contents (a
     SHA-256 "hash"). A hash is a short string calculated from every byte in a
     file. Identical files always produce the same hash; files that differ by
     even one byte produce completely different ones.

Files that share both a size and a fingerprint are reported as duplicates.

USAGE
-----
    python dupe_finder.py ~/Downloads
    python dupe_finder.py ~/Pictures --min-size 1MB
    python dupe_finder.py ~/Documents --include-hidden
"""

# This line lets us write modern type hints (like list[str]) on older Python
# versions. It changes nothing about how the program behaves.
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# When fingerprinting a file we read it in pieces of this size rather than all
# at once. That way a 20 GB video uses the same small amount of memory as a
# 20 KB note. 1 MiB is a comfortable middle ground between speed and memory.
CHUNK_SIZE = 1024 * 1024

# Multipliers for sizes typed like "10KB". Computers count in powers of 1024.
SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------
# A "dataclass" is a lightweight way to bundle related values under one name,
# so the rest of the code can say `group.size` instead of juggling tuples.


@dataclass
class DuplicateGroup:
    """One set of files that all have identical contents."""

    size: int  # size of each file, in bytes
    digest: str  # the shared fingerprint
    paths: list[Path]  # every location where this content appears

    @property
    def wasted_bytes(self) -> int:
        """Space you'd recover by keeping one copy and removing the rest."""
        return self.size * (len(self.paths) - 1)


@dataclass
class ScanResult:
    """Everything a scan produces: the answer, plus anything that went wrong."""

    groups: list[DuplicateGroup] = field(default_factory=list)
    # How many files were actually compared (those that passed the filters).
    files_examined: int = 0
    # Files we could not read (permissions, deleted mid-scan, and so on). We
    # collect them to report at the end instead of stopping the whole scan.
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Step 0: walking the folder tree
# ---------------------------------------------------------------------------


def walk_files(root: Path, include_hidden: bool) -> Iterator[Path]:
    """Yield every regular file under `root`, in a predictable order.

    Skipped on purpose:
      * Hidden files and folders (names starting with "."), unless asked for.
        Folders like ".git" hold thousands of internal files that you almost
        never want to dedupe by hand.
      * Symbolic links (shortcuts). A shortcut and the file it points to would
        look like duplicates, but "removing the duplicate" would break things.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        # Editing `dirnames` in place tells os.walk which subfolders to enter.
        # Sorting makes the output order the same on every run.
        if include_hidden:
            dirnames[:] = sorted(dirnames)
        else:
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))

        for name in sorted(filenames):
            if not include_hidden and name.startswith("."):
                continue
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            yield path


# ---------------------------------------------------------------------------
# Step 1: group by size (cheap)
# ---------------------------------------------------------------------------


def group_by_size(
    root: Path, min_size: int, include_hidden: bool
) -> tuple[dict[int, list[Path]], int, list[str]]:
    """Bucket files by size.

    Returns (buckets, number of files examined, list of problems).
    """
    buckets: dict[int, list[Path]] = defaultdict(list)
    problems: list[str] = []
    files_examined = 0

    # A "hard link" is a second name for the very same data on disk. Two hard
    # links look like two identical files, but deleting one frees no space at
    # all. Every file on disk has a unique (device, inode) ID, so we remember
    # the IDs we've seen and skip repeats.
    seen_ids: set[tuple[int, int]] = set()

    for path in walk_files(root, include_hidden):
        try:
            info = path.stat()
        except OSError as err:
            problems.append(f"{path}: {err.strerror}")
            continue

        if info.st_size < min_size:
            continue

        # An inode of 0 means the filesystem doesn't provide real IDs, so the
        # hard-link check isn't possible and we skip it.
        if info.st_ino != 0:
            file_id = (info.st_dev, info.st_ino)
            if file_id in seen_ids:
                continue
            seen_ids.add(file_id)

        files_examined += 1
        buckets[info.st_size].append(path)

    return buckets, files_examined, problems


# ---------------------------------------------------------------------------
# Step 2: fingerprint (expensive, so only for files that share a size)
# ---------------------------------------------------------------------------


def hash_file(path: Path) -> str:
    """Return the SHA-256 fingerprint of a file's contents as a hex string."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:  # "rb" = read raw bytes, not text
        # The `:=` ("walrus") operator reads a chunk AND tests it in one step.
        # An empty chunk means we've reached the end of the file.
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def find_duplicates(
    root: Path, min_size: int = 1, include_hidden: bool = False
) -> ScanResult:
    """Scan `root` and return every set of identical files.

    `min_size` defaults to 1 byte so empty files are ignored: all empty files
    are technically identical, which is true but not useful.
    """
    buckets, files_examined, problems = group_by_size(root, min_size, include_hidden)
    result = ScanResult(files_examined=files_examined, problems=problems)

    for size, paths in buckets.items():
        # A file alone in its size bucket has no possible twin. Skip it.
        if len(paths) < 2:
            continue

        # Within this bucket, sort files by fingerprint.
        by_digest: dict[str, list[Path]] = defaultdict(list)
        for path in paths:
            try:
                by_digest[hash_file(path)].append(path)
            except OSError as err:
                result.problems.append(f"{path}: {err.strerror}")

        for digest, same_content in by_digest.items():
            if len(same_content) >= 2:
                result.groups.append(DuplicateGroup(size, digest, same_content))

    # Biggest savings first, so the most worthwhile clean-ups are at the top.
    # Ties are broken by path so the order never changes between runs.
    result.groups.sort(key=lambda g: (-g.wasted_bytes, str(g.paths[0])))
    return result


# ---------------------------------------------------------------------------
# Presenting results
# ---------------------------------------------------------------------------


def human_size(num_bytes: int) -> str:
    """Turn 1536 into '1.5 KB' so people can read it at a glance."""
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    if index == 0:
        return f"{int(size)} B"
    return f"{size:.1f} {units[index]}"


def parse_size(text: str) -> int:
    """Turn a typed size like '10KB' or '1.5 MB' into a number of bytes.

    Raises argparse.ArgumentTypeError (which argparse shows as a friendly
    message) if the text isn't a valid size.
    """
    cleaned = text.strip().upper().replace(" ", "")
    # Peel the letters off the end; whatever is left should be the number.
    number_part = cleaned.rstrip("BKMG")
    unit = cleaned[len(number_part):] or "B"

    try:
        value = float(number_part)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a size: {text!r} (try 10KB or 2MB)")
    if value < 0 or unit not in SIZE_UNITS:
        raise argparse.ArgumentTypeError(f"not a size: {text!r} (try 10KB or 2MB)")

    return int(value * SIZE_UNITS[unit])


def format_report(result: ScanResult, root: Path) -> str:
    """Build the human-readable report as one block of text."""
    # "Checked" (not "scanned") because files skipped by the filters (empty,
    # hidden, below --min-size) are not counted here.
    lines = [f"Checked {result.files_examined:,} files in {root}"]

    if not result.groups:
        lines.append("No duplicate files found.")
        return "\n".join(lines)

    extra_copies = sum(len(g.paths) - 1 for g in result.groups)
    total_wasted = sum(g.wasted_bytes for g in result.groups)
    lines.append(
        f"Found {len(result.groups)} set(s) of duplicates: "
        f"{extra_copies} extra cop{'y' if extra_copies == 1 else 'ies'}, "
        f"{human_size(total_wasted)} reclaimable."
    )

    for number, group in enumerate(result.groups, start=1):
        lines.append("")
        lines.append(
            f"[{number}] {len(group.paths)} copies of a {human_size(group.size)} file "
            f"({human_size(group.wasted_bytes)} reclaimable)"
        )
        lines.extend(f"      {path}" for path in group.paths)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse the command line, run the scan, print the report.

    Returns 0 on success and 1 on a usage error. (The number returned here is
    the program's "exit code", which other tools can check.)
    """
    parser = argparse.ArgumentParser(
        description="Find duplicate files. Reports only; never deletes anything."
    )
    parser.add_argument("folder", type=Path, help="folder to search")
    parser.add_argument(
        "--min-size",
        type=parse_size,
        default=1,
        metavar="SIZE",
        help="ignore files smaller than this, e.g. 10KB or 2MB (default: ignore only empty files)",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="also search hidden files and folders (names starting with '.')",
    )
    args = parser.parse_args(argv)

    if not args.folder.is_dir():
        print(f"Not a folder: {args.folder}", file=sys.stderr)
        return 1

    result = find_duplicates(args.folder, args.min_size, args.include_hidden)
    print(format_report(result, args.folder))

    # Problems go to "stderr", a separate output channel for warnings, so they
    # don't get mixed into the report if someone saves the report to a file.
    for problem in result.problems:
        print(f"Warning: could not read {problem}", file=sys.stderr)

    return 0


# This guard means "only run main() when the file is executed directly", not
# when another file (like the tests) merely imports it.
if __name__ == "__main__":
    sys.exit(main())
