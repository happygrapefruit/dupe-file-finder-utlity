# dupe-finder

A command-line tool that finds files with identical contents, even when their names
differ, and tells you how much disk space you'd get back by keeping just one copy.

**It only reports. It never deletes or moves anything.**

Written in plain Python with no dependencies. The code is heavily commented so it can
be read top to bottom by someone who doesn't write code.

## Try it

```
python dupe_finder.py ~/Downloads
python dupe_finder.py ~/Pictures --min-size 1MB
python dupe_finder.py ~/Documents --include-hidden
```

Example output:

```
Checked 8 files in Downloads
Found 2 set(s) of duplicates: 3 extra copies, 2.9 MB reclaimable.

[1] 2 copies of a 2.9 MB file (2.9 MB reclaimable)
      Downloads/vacation.mov
      Downloads/old backup/vacation copy.mov

[2] 3 copies of a 20 B file (40 B reclaimable)
      Downloads/budget (1).txt
      Downloads/budget.txt
      Downloads/old backup/budget_final.txt
```

| Option | What it does |
| --- | --- |
| `--min-size SIZE` | Ignore files smaller than this, e.g. `10KB` or `2MB`. By default only empty files are ignored. |
| `--include-hidden` | Also search hidden files and folders (names starting with `.`). |

## How it works

Comparing every file with every other file would be very slow, so the search narrows
things down, cheapest check first:

1. **Group by size.** Files of different sizes can't be identical, and a file's size is
   nearly free to look up. Most files are ruled out here without being opened.
2. **Fingerprint the rest.** Files that share a size get a SHA-256 hash, a short string
   calculated from every byte of the file. Identical content always gives the same
   hash; a single changed byte gives a completely different one.

Files sharing both a size and a fingerprint are reported as duplicates.

## What to look for when reading the code

Everything is in [`dupe_finder.py`](dupe_finder.py), in the order the work happens:

- **`walk_files`** skips hidden files and shortcuts (symlinks) on purpose, and the
  comments explain why.
- **`group_by_size`** also skips *hard links*: two names for the same data on disk.
  Removing one frees no space, so reporting them would be misleading.
- **`hash_file`** reads in 1 MB pieces so huge files don't use huge amounts of memory.
- **`find_duplicates`** ties the steps together and sorts results by space saved.
- **Error handling:** a file that can't be read is noted as a warning and the scan keeps
  going, instead of crashing halfway through a large folder.

## Tests

```
python -m unittest -v
```

22 tests cover the behaviors above. [`test_dupe_finder.py`](test_dupe_finder.py) is a
readable list of what the tool promises to do, including "never deletes anything".

## Limits

- Finds **exact** copies only. Two photos that look the same but were saved at different
  quality settings are different files and won't be matched.
- Matching by hash means two different files colliding is theoretically possible but, with
  SHA-256, not something that happens in practice.
