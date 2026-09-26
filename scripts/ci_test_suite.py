"""Run every discovered unittest, partitioned by module across CI workers."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    args = parser.parse_args()
    if args.shards < 1 or not 0 <= args.shard < args.shards:
        parser.error("shard must be between zero and shards minus one")
    sys.path.insert(0, str(ROOT))
    discovered = list(cases(unittest.defaultTestLoader.discover(str(ROOT / "tests"))))
    modules = sorted({test.__class__.__module__ for test in discovered})
    selected_modules = set(modules[args.shard::args.shards])
    selected = [test for test in discovered if test.__class__.__module__ in selected_modules]
    if not selected:
        parser.error("selected shard contains no tests")
    print(f"Shard {args.shard + 1}/{args.shards}: {len(selected)}/{len(discovered)} tests, "
          f"{len(selected_modules)}/{len(modules)} modules", flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
