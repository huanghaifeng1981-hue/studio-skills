"""Unit tests for studio-checkin-video scripts/day_queue.py."""
from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "day_queue.py"


def run_q(db: Path, *args: str) -> dict:
    res = subprocess.run(
        [sys.executable, str(SCRIPT), str(db), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(res.stdout.strip())


class DayQueueTests(unittest.TestCase):
    def test_batch_priority_and_full_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "runs" / "queue.db"
            # 1. Init two orders: match runs for ALL orders before copy starts.
            self.assertEqual(run_q(db, "init", "ORD1", "ORD2"), {"action": "match", "order": "ORD1"})
            # ORD1 already has video -> jumps straight to report, so ORD2 still runs match first!
            self.assertEqual(run_q(db, "advance", "ORD1", "match", "report"), {"action": "match", "order": "ORD2"})
            # ORD2 needs new video -> goes to copy (priority 1, before ORD1's report priority 2).
            self.assertEqual(run_q(db, "advance", "ORD2", "match", "copy"), {"action": "copy", "order": "ORD2"})
            # After ORD2 finishes copy -> select (priority 2), ORD1 is earlier in rowid so ORD1 runs report!
            self.assertEqual(run_q(db, "advance", "ORD2", "copy", "select"), {"action": "report", "order": "ORD1"})
            self.assertEqual(run_q(db, "advance", "ORD1", "report", "done"), {"action": "select", "order": "ORD2"})

            for cur, nxt in [("select", "edit"), ("edit", "archive"), ("archive", "report"), ("report", "clean"), ("clean", "done")]:
                out = run_q(db, "advance", "ORD2", cur, nxt)
            self.assertEqual(out, {"action": "check_report"})

    def test_block_and_resume_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.db"
            run_q(db, "init", "A", "B")
            # Block A, immediately advances to B
            self.assertEqual(run_q(db, "block", "A", "NAS目录缺失"), {"action": "match", "order": "B"})
            run_q(db, "advance", "B", "match", "report")
            out = run_q(db, "advance", "B", "report", "done")
            self.assertEqual(out["action"], "blocked")
            self.assertEqual(out["orders"], [["A", "match", "NAS目录缺失"]])

            # Resume A restores exact stage
            self.assertEqual(run_q(db, "resume", "A"), {"action": "match", "order": "A"})


if __name__ == "__main__":
    unittest.main()
