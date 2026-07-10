from __future__ import annotations

import unittest
from unittest.mock import patch

from backend import mc_core
from backend.models import SearchPlan, Target
from backend.planner import _augment_relative_layout_from_message, _normalize_plan_data


def target(kind: str, target_id: str, label: str) -> Target:
    return Target(kind=kind, id=target_id, label=label)


class PlannerRelativeLayoutTests(unittest.TestCase):
    def test_normalizer_materializes_layout_and_distance_edges(self) -> None:
        normalized, warnings = _normalize_plan_data(
            {
                "targets": [
                    {"kind": "structure", "id": "village"},
                    {"kind": "biome", "id": "cherry_grove"},
                    {"kind": "biome", "id": "ocean"},
                ],
                "relative_layout": {
                    "center": "village",
                    "back": "cherry_grove",
                    "front": "ocean",
                    "relation": "opposite_sides",
                    "min_angle": 135,
                },
            }
        )

        self.assertEqual(normalized["relative_layout"][0]["min_angle"], 135)
        pairs = {
            frozenset((item["a"], item["b"])): item["threshold"]
            for item in normalized["adjacency"]
        }
        self.assertEqual(pairs[frozenset(("village", "cherry_grove"))], 512)
        self.assertEqual(pairs[frozenset(("village", "ocean"))], 512)
        self.assertEqual(warnings, [])

    def test_compatibility_version_does_not_emit_routine_warning(self) -> None:
        mapped_version, mode, warnings = mc_core._resolve_version("26.2")

        self.assertEqual(mapped_version, "1.21.3")
        self.assertEqual(mode, "compatibility")
        self.assertEqual(warnings, [])

    def test_chinese_back_and_front_phrase_is_enforced(self) -> None:
        plan = SearchPlan(targets=[], capability="unsupported", unsupported_reason="不支持朝向")
        result = _augment_relative_layout_from_message(
            "找一个村庄背靠樱花林，并且村庄坐落于草原，并且面朝大海的地方",
            plan,
        )

        self.assertEqual({item.id for item in result.targets}, {"village", "plains", "cherry_grove", "ocean"})
        self.assertEqual(result.anchor_target_id, "village")
        self.assertEqual(result.capability, "supported")
        self.assertEqual(result.relative_layout[0]["min_angle"], 120)
        self.assertTrue(any(item.get("relation") == "same_biome" for item in result.adjacency))

    def test_following_nearby_clause_does_not_replace_the_front_target(self) -> None:
        result = _augment_relative_layout_from_message(
            "找一个村庄背靠樱花林，并且村庄坐落于草原，并且面朝大海，并且村庄附近800格内有女巫小屋",
            SearchPlan(targets=[], capability="supported"),
        )

        self.assertEqual(result.relative_layout[0]["back"], "cherry_grove")
        self.assertEqual(result.relative_layout[0]["front"], "ocean")
        self.assertIn("witch_hut", {item.id for item in result.targets})
        witch_edges = [
            item
            for item in result.adjacency
            if {str(item.get("a")), str(item.get("b"))} == {"village", "witch_hut"}
        ]
        self.assertEqual(witch_edges[0]["threshold"], 800)


class SearchRelativeLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = SearchPlan(
            targets=[
                target("structure", "village", "村庄"),
                target("biome", "plains", "平原"),
                target("biome", "cherry_grove", "樱花林"),
                target("biome", "ocean", "海洋"),
            ],
            adjacency=[
                {"a": "village", "b": "plains", "threshold": 0, "relation": "same_biome"},
                {"a": "village", "b": "cherry_grove", "threshold": 512, "relation": "near"},
                {"a": "village", "b": "ocean", "threshold": 512, "relation": "near"},
            ],
            relative_layout=[
                {
                    "center": "village",
                    "back": "cherry_grove",
                    "front": "ocean",
                    "relation": "opposite_sides",
                    "min_angle": 120,
                    "back_max_distance": 512,
                    "front_max_distance": 512,
                }
            ],
            anchor_target_id="village",
        )

    def point(self, kind: str, target_id: str, label: str, x: int, z: int) -> dict:
        return {"kind": kind, "id": target_id, "label": label, "x": x, "z": z}

    def test_opposite_points_pass_and_same_side_points_fail(self) -> None:
        village = self.point("structure", "village", "村庄", 0, 0)
        cherry = self.point("biome", "cherry_grove", "樱花林", -200, 0)
        ocean = self.point("biome", "ocean", "海洋", 200, 0)
        same_side_ocean = self.point("biome", "ocean", "海洋", -200, 80)

        self.assertEqual(mc_core._relative_layout_angle(village, cherry, ocean), 180.0)
        self.assertTrue(mc_core._point_fits_selected(self.plan, ocean, [village, cherry]))
        self.assertFalse(mc_core._point_fits_selected(self.plan, same_side_ocean, [village, cherry]))

    @patch("backend.mc_core._call_biome_at_many")
    def test_cluster_result_contains_auditable_layout_check(self, biome_at_many) -> None:
        biome_at_many.return_value = ({(0, 0): "plains"}, [])
        points = [
            self.point("structure", "village", "村庄", 0, 0),
            self.point("biome", "plains", "平原", 0, 0),
            self.point("biome", "cherry_grove", "樱花林", -200, 0),
            self.point("biome", "ocean", "海洋", 200, 0),
        ]

        result = mc_core._evaluate_cluster("0", "1.21.3", 0, 0, 4096, self.plan, points)

        self.assertTrue(result["satisfied"])
        self.assertEqual(result["layout_checks"][0]["angle"], 180.0)
        self.assertTrue(result["layout_checks"][0]["passed"])
        self.assertEqual(len(result["adjacency_checks"]), 2)
        self.assertTrue(all(item["passed"] for item in result["adjacency_checks"]))
if __name__ == "__main__":
    unittest.main()
