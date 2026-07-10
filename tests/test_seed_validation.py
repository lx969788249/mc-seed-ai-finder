from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
NATIVE = ROOT / "native" / "mc_query"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "seed_validation.json"


def native_json(*args: object) -> dict:
    completed = subprocess.run(
        [str(NATIVE), *(str(arg) for arg in args)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(completed.stdout)


class SeedValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        if not NATIVE.exists():
            raise RuntimeError("native/mc_query is missing; run `make native` first")

    def test_known_surface_biomes(self) -> None:
        version = self.fixture["minecraft_version"]
        for case in self.fixture["biome_cases"]:
            with self.subTest(case=case["name"]):
                result = native_json("biome_at", version, case["seed"], case["x"], case["z"])
                self.assertTrue(result["ok"])
                self.assertEqual(result["projection"], "surface")
                self.assertEqual(result["biome"]["name"], case["expected_biome"])
                self.assertGreaterEqual(result["surface_height"], case["height_min"])
                self.assertLessEqual(result["surface_height"], case["height_max"])

    def test_known_nearest_structures(self) -> None:
        version = self.fixture["minecraft_version"]
        for case in self.fixture["structure_cases"]:
            with self.subTest(case=case["name"]):
                expected = [tuple(point) for point in case["expected"]]
                result = native_json(
                    "structure",
                    version,
                    case["seed"],
                    case["structure"],
                    case["center_x"],
                    case["center_z"],
                    case["radius"],
                    len(expected),
                )
                actual = [(point["x"], point["z"]) for point in result["results"]]
                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
