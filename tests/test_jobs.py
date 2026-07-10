from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import database, jobs, progress


class PersistentJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "jobs.sqlite3"
        self.db_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        database.init_db()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temporary.cleanup()

    def test_secret_is_encrypted_separately_from_payload(self) -> None:
        created = jobs.create_job(
            "search",
            {"query": "找村庄", "seed": "0"},
            secret="sk-job-secret-value",
        )

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload_json, secret_enc, status FROM search_jobs WHERE id=?",
                (created["id"],),
            ).fetchone()
        self.assertNotIn("sk-job-secret-value", row[0])
        self.assertNotEqual(row[1], "sk-job-secret-value")
        self.assertEqual(row[2], "queued")
        self.assertEqual(jobs.get_job(created["id"], include_payload=True)["secret"], "sk-job-secret-value")

    def test_running_job_is_requeued_after_restart(self) -> None:
        created = jobs.create_job("search", {"query": "找村庄"})
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE search_jobs SET status='running' WHERE id=?", (created["id"],))
            conn.commit()

        recovered = jobs.recover_interrupted_jobs(stale_seconds=0)

        self.assertEqual(recovered, 1)
        self.assertEqual(jobs.get_job(created["id"])["status"], "queued")

    def test_progress_is_persisted_and_survives_memory_loss(self) -> None:
        created = jobs.create_job("search", {"query": "找村庄"})
        progress.start_progress(created["id"], "开始")
        progress.update_progress(created["id"], stage="search", checked=12, total=30)
        with progress._LOCK:
            progress._PROGRESS.pop(created["id"], None)

        snapshot = progress.get_progress(created["id"])

        self.assertEqual(snapshot["stage"], "search")
        self.assertEqual(snapshot["checked"], 12)
        self.assertEqual(snapshot["total"], 30)

    def test_queued_job_can_be_cancelled(self) -> None:
        created = jobs.create_job("search", {"query": "找村庄"})

        cancelled = jobs.cancel_job(created["id"])

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["cancel_requested"])


if __name__ == "__main__":
    unittest.main()
