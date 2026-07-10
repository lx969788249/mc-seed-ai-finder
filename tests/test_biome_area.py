from __future__ import annotations

import unittest
from unittest.mock import patch

from backend import mc_core
from backend.models import SearchPlan, Target
from backend.planner import _augment_biome_area_from_message


def biome(target_id: str, label: str) -> Target:
    return Target(kind="biome", id=target_id, label=label)


class BiomeAreaPlannerTests(unittest.TestCase):
    def test_largest_biome_becomes_approximate_area_search(self) -> None:
        plan = SearchPlan(
            targets=[biome("mushroom_fields", "蘑菇岛")],
            objective="largest_area",
            capability="unsupported",
        )

        result = _augment_biome_area_from_message("帮我找出这个种子里面最大的蘑菇岛", plan)

        self.assertEqual(result.objective, "largest_area")
        self.assertEqual(result.sort_by, "largest_area")
        self.assertEqual(result.capability, "approximate")
        self.assertFalse(result.nearest_to_player)
        self.assertEqual(
            result.biome_area_constraints,
            [{"target": "mushroom_fields", "min_area": None, "preference": "largest"}],
        )

    def test_vague_large_ocean_gets_default_minimum(self) -> None:
        plan = SearchPlan(
            targets=[
                Target(kind="structure", id="village", label="村庄"),
                biome("ocean", "海洋"),
            ],
            adjacency=[{"a": "village", "b": "ocean", "threshold": 1000}],
        )

        result = _augment_biome_area_from_message("找一个靠近大海的村庄，大海要大一点", plan)

        self.assertEqual(result.capability, "approximate")
        self.assertEqual(result.biome_area_constraints[0]["target"], "ocean")
        self.assertEqual(result.biome_area_constraints[0]["min_area"], 1_000_000)

    def test_explicit_area_is_preserved(self) -> None:
        plan = SearchPlan(targets=[biome("ocean", "海洋")])

        result = _augment_biome_area_from_message("找面积至少 250 万平方格的大海", plan)

        self.assertEqual(result.biome_area_constraints[0]["min_area"], 2_500_000)


class BiomeAreaEvaluationTests(unittest.TestCase):
    @patch("backend.mc_core._measure_biome_point")
    def test_area_constraint_is_auditable(self, measure) -> None:
        measure.return_value = (
            {
                "area": 2_400_000,
                "step": 64,
                "radius": 4096,
                "closed": True,
                "truncated": False,
                "bounds": {"min_x": -1000, "max_x": 1000, "min_z": -1000, "max_z": 1000},
                "center": {"x": 0, "z": 0},
                "width": 2001,
                "height": 2001,
                "perimeter": 9000,
                "sample_y": 63,
            },
            [],
        )
        plan = SearchPlan(
            targets=[biome("ocean", "海洋")],
            biome_area_constraints=[{"target": "ocean", "min_area": 1_000_000, "preference": "larger"}],
        )
        point = {"id": "ocean", "kind": "biome", "label": "海洋", "x": 32, "z": 64}

        checks, failures, warnings = mc_core._evaluate_biome_area_constraints("0", "1.21.3", plan, [point])

        self.assertEqual(failures, [])
        self.assertEqual(warnings, [])
        self.assertTrue(checks[0]["passed"])
        self.assertEqual(checks[0]["area"], 2_400_000)
        self.assertEqual(checks[0]["sample_y"], 63)
        self.assertEqual(point["biome_area"], 2_400_000)

    def test_largest_area_sort_uses_measured_area_first(self) -> None:
        plan = SearchPlan(
            targets=[biome("mushroom_fields", "蘑菇岛")],
            sort_by="largest_area",
        )
        small = {
            "distance": 10,
            "max_pairwise_distance": 0,
            "targets": [{"id": "mushroom_fields", "x": 0, "z": 0}],
            "biome_area_checks": [{"area": 100_000}],
        }
        large = {
            "distance": 1000,
            "max_pairwise_distance": 0,
            "targets": [{"id": "mushroom_fields", "x": 1000, "z": 0}],
            "biome_area_checks": [{"area": 500_000}],
        }

        ranked = sorted([small, large], key=lambda item: mc_core._cluster_sort_key(item, 0, 0, plan))

        self.assertIs(ranked[0], large)


if __name__ == "__main__":
    unittest.main()
