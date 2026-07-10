from __future__ import annotations

import unittest

from backend import mc_core


class SurfaceMapTests(unittest.TestCase):
    def test_surface_biome_replaces_cave_slice_at_cherry_grove(self) -> None:
        biome, warnings = mc_core._call_biome_at("0", "1.21.3", -1024, 1344)

        self.assertEqual(warnings, [])
        self.assertEqual(biome, "cherry_grove")

    def test_map_contains_surface_heights_and_no_cave_projection(self) -> None:
        data, warnings = mc_core.generate_map("0", "1.21.3", -1024, 1344, 1024, 65)

        self.assertEqual(warnings, [])
        self.assertTrue(data["ok"])
        self.assertEqual(data["projection"], "surface")
        self.assertEqual(data["height_mode"], "approximate")
        self.assertEqual(len(data["heights"]), data["size"] * data["size"])
        self.assertLess(data["height_min"], data["height_max"])

        names = [data["biomes"][index]["name"] for index in data["cells"]]
        self.assertIn("cherry_grove", names)
        self.assertTrue({"lush_caves", "dripstone_caves", "deep_dark"}.isdisjoint(names))


if __name__ == "__main__":
    unittest.main()
