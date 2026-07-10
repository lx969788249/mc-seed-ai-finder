from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend import database, evolution, main
from backend.models import SearchPlan, Target, UnmetFeedbackIn


class EvolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.db_path = root / "test.sqlite3"
        self.report_dir = root / "evolution"
        self.db_patch = patch.object(database, "DB_PATH", self.db_path)
        self.dir_patch = patch.object(evolution, "EVOLUTION_DIR", self.report_dir)
        self.db_patch.start()
        self.dir_patch.start()
        database.init_db()

    def tearDown(self) -> None:
        self.dir_patch.stop()
        self.db_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def plan() -> dict:
        return {
            "targets": [{"kind": "biome", "id": "cherry_grove", "label": "樱花林"}],
            "objective": "unsupported_metric",
            "capability": "unsupported",
            "unsupported_reason": "尚未实现地形完整度评分",
            "adjacency": [],
        }

    def test_record_redacts_secrets_and_deduplicates_request(self) -> None:
        query = "帮我分析樱花林，api_key=sk-super-secret-value-12345"
        first = evolution.record_unmet_request(
            query,
            source="automatic",
            reason_code="unsupported_capability",
            request_id="req-1",
            plan=self.plan(),
            context={"api_key": "must-not-store", "version": "1.21.3"},
        )
        second = evolution.record_unmet_request(
            query,
            source="automatic",
            reason_code="unsupported_capability",
            request_id="req-1",
            plan=self.plan(),
        )

        self.assertTrue(first)
        self.assertFalse(second)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT query_text, context_json FROM unmet_request_events"
            ).fetchone()
        self.assertIn("[REDACTED]", row[0])
        self.assertNotIn("super-secret", row[0])
        self.assertNotIn("api_key", row[1])
        self.assertEqual(json.loads(row[1]), {"version": "1.21.3"})

    def test_nightly_cycle_clusters_plans_and_weights_feedback(self) -> None:
        evolution.record_unmet_request(
            "找树冠最完整的樱花林",
            source="automatic",
            reason_code="unsupported_capability",
            reason_detail="尚未实现树冠评分",
            request_id="req-auto",
            planner="deepseek_json",
            plan=self.plan(),
        )
        evolution.record_unmet_request(
            "这个樱花林结果没有解决我的问题",
            source="user_feedback",
            reason_code="user_report",
            reason_detail="not_solved",
            request_id="req-feedback",
            planner="deepseek_json",
            plan=self.plan(),
        )

        report = evolution.run_evolution_cycle(force=True)

        self.assertEqual(report["event_count"], 2)
        self.assertEqual(report["backlog_count"], 1)
        item = report["items"][0]
        self.assertEqual(item["occurrence_count"], 2)
        self.assertEqual(item["feedback_count"], 1)
        self.assertGreater(item["priority_score"], 20)
        self.assertIn("isolated_branch_or_worktree", item["required_gates"])
        self.assertTrue((self.report_dir / "latest.json").exists())
        self.assertIn("Evolution Backlog", (self.report_dir / "latest.md").read_text(encoding="utf-8"))

    def test_search_activity_counter_never_goes_negative(self) -> None:
        while evolution.active_search_count():
            evolution.search_activity_finished()
        evolution.search_activity_started()
        evolution.search_activity_started()
        evolution.search_activity_finished()
        evolution.search_activity_finished()
        evolution.search_activity_finished()
        self.assertEqual(evolution.active_search_count(), 0)


class EvolutionCaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "test.sqlite3"
        self.db_patch = patch.object(database, "DB_PATH", self.db_path)
        self.db_patch.start()
        database.init_db()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def settings() -> dict:
        return {
            "seed": "0",
            "version": "1.21.3",
            "center_x": 0,
            "center_z": 0,
            "search_radius": 30_000_000,
            "max_results": 1,
            "deepseek_api_key": "test-key",
            "deepseek_base_url": "https://api.deepseek.com",
            "deepseek_model": "deepseek-v4-flash",
        }

    async def test_unsupported_search_is_captured_automatically(self) -> None:
        plan = SearchPlan(
            targets=[Target(kind="biome", id="cherry_grove", label="樱花林")],
            objective="unsupported_metric",
            capability="unsupported",
            unsupported_reason="尚未实现树冠完整度",
        )
        with patch.object(main, "deepseek_plan", new=AsyncMock(return_value=("deepseek_json", plan, []))):
            response = await main._run_query(
                "找树冠最完整的樱花林",
                self.settings(),
                "capture-auto",
            )

        self.assertEqual(response.results, [])
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT source, reason_code FROM unmet_request_events"
            ).fetchone()
        self.assertEqual(row, ("automatic", "unsupported_capability"))

    async def test_feedback_endpoint_records_anonymous_signal(self) -> None:
        payload = UnmetFeedbackIn(
            query="这个结果没有解决问题",
            request_id="capture-feedback",
            planner="deepseek_json",
            plan={"targets": [], "objective": "nearest"},
        )

        result = await main.unmet_feedback(payload, None)

        self.assertTrue(result["ok"])
        self.assertTrue(result["recorded"])
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT source, reason_code FROM unmet_request_events"
            ).fetchone()
        self.assertEqual(row, ("user_feedback", "user_report"))


if __name__ == "__main__":
    unittest.main()
