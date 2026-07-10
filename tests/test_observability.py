from __future__ import annotations

import unittest

from backend.observability import observe_request, observe_search_stage, render_metrics


class ObservabilityTests(unittest.TestCase):
    def test_prometheus_output_contains_core_signals(self) -> None:
        observe_request("GET", "/health/live", 200, 0.01)
        observe_search_stage("native", "results", 0.25)

        output = render_metrics(
            active_searches=2,
            jobs={"queued": 3, "running": 1},
            map_cache={"hits": 5, "misses": 2, "writes": 2, "errors": 0, "files": 2, "bytes": 1024},
        )

        self.assertIn("mc_active_searches 2", output)
        self.assertIn('mc_search_jobs{status="queued"} 3', output)
        self.assertIn("mc_map_cache_hits 5", output)
        self.assertIn('stage="native",outcome="results"', output)


if __name__ == "__main__":
    unittest.main()
