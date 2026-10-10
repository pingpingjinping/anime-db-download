#!/usr/bin/env python3
"""Fail a first-day refresh if submitted Pi reviews were not in db-v1."""
import argparse
import pathlib
import sqlite3
import tempfile
import sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from scripts.split_db_v1_release import extract_database


def check(archive, month):
    with tempfile.TemporaryDirectory(prefix="monthly-reviewed-check-") as tmp:
        db_path=pathlib.Path(tmp)/"db.sqlite"
        extract_database(archive,db_path)
        with sqlite3.connect(db_path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pi_reviewed_work_groups_meta'").fetchone():
                raise ValueError("Pi review asset exists, but release lacks reviewed group overlay")
            row=db.execute("SELECT value FROM pi_reviewed_work_groups_meta WHERE key='reviewed_month'").fetchone()
            if not row or row[0] != month:
                raise ValueError("Wrong Pi-reviewed overlay month; expected " + month)
            count=db.execute("SELECT COUNT(*) FROM pi_reviewed_work_groups").fetchone()[0]
            print("Validated release reviewed group overlay for",month,"groups:",count)
            return count


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--archive",required=True)
    parser.add_argument("--month",required=True)
    args=parser.parse_args()
    check(args.archive,args.month)


if __name__=="__main__": main()
