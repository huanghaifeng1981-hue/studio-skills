"""Local SQLite queue for one ERP selection date. One active operator drives the GUI for the batch."""
from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path

STAGES = ("match", "copy", "select", "edit", "archive", "report", "clean", "done")
TRANSITIONS = {
    "match": ("copy", "report"),
    "copy": ("select",),
    "select": ("edit",),
    "edit": ("archive",),
    "archive": ("report",),
    "report": ("clean", "done"),
    "clean": ("done",),
}


def next_action(db: sqlite3.Connection) -> dict:
    row = db.execute("""
        SELECT order_key, stage FROM jobs WHERE obstacle = '' AND stage <> 'done'
        ORDER BY CASE stage WHEN 'match' THEN 0 WHEN 'copy' THEN 1 ELSE 2 END,
                 rowid LIMIT 1
    """).fetchone()
    if row:
        return {"action": row[1], "order": row[0]}
    blocked = list(db.execute("SELECT order_key, stage, obstacle FROM jobs WHERE obstacle <> '' ORDER BY rowid"))
    return {"action": "blocked", "orders": blocked} if blocked else {"action": "check_report"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init").add_argument("keys", nargs="+")
    commands.add_parser("next")
    step = commands.add_parser("advance")
    step.add_argument("key")
    step.add_argument("stage", choices=STAGES)
    step.add_argument("target", choices=STAGES)
    block = commands.add_parser("block")
    block.add_argument("key")
    block.add_argument("reason")
    commands.add_parser("resume").add_argument("key")
    args = parser.parse_args()

    if args.command != "init" and not args.database.is_file():
        parser.error("当日队列尚未建立")
    if args.command == "init":
        args.database.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(args.database) as db:
        db.execute("BEGIN IMMEDIATE")
        if args.command == "init":
            db.execute("CREATE TABLE IF NOT EXISTS jobs (order_key TEXT PRIMARY KEY, stage TEXT, obstacle TEXT)")
            existing = [r[0] for r in db.execute("SELECT order_key FROM jobs ORDER BY rowid")]
            if len(set(args.keys)) != len(args.keys) or (existing and existing != args.keys):
                parser.error("排期清单重复或与已存批次不一致")
            if not existing:
                db.executemany("INSERT INTO jobs VALUES (?, 'match', '')", [(k,) for k in args.keys])
        elif args.command != "next":
            row = db.execute("SELECT stage, obstacle FROM jobs WHERE order_key=?", (args.key,)).fetchone()
            if not row or row[0] == "done":
                parser.error("单号无效或已结束")
            stage, obstacle = row
            if args.command == "advance":
                if obstacle or stage != args.stage or args.target not in TRANSITIONS.get(stage, ()):
                    parser.error("阶段与断点不符")
                stage = args.target
            elif args.command == "block":
                obstacle = args.reason.strip()
                if not obstacle:
                    parser.error("请记录具体障碍")
            else:
                obstacle = ""
            db.execute("UPDATE jobs SET stage=?, obstacle=? WHERE order_key=?", (stage, obstacle, args.key))
        print(json.dumps(next_action(db), ensure_ascii=False))


if __name__ == "__main__":
    main()
