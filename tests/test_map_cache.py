from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import map_cache


class MapCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.temporary.name)
        self.cache_patch = patch.object(map_cache, "CACHE_DIR", self.cache_dir)
        self.cache_patch.start()

    def tearDown(self) -> None:
        self.cache_patch.stop()
        self.temporary.cleanup()

    def test_second_request_uses_persistent_cache(self) -> None:
        calls = 0

        def builder():
            nonlocal calls
            calls += 1
            return {"ok": True, "cells": [1, 2, 3]}, ["notice"]

        params = {"seed": "0", "version": "1.21.3", "center_x": 0, "center_z": 0, "radius": 64, "size": 16}
        first = map_cache.get_or_create(params, builder)
        second = map_cache.get_or_create(params, builder)

        self.assertEqual(calls, 1)
        self.assertFalse(first[2]["hit"])
        self.assertTrue(second[2]["hit"])
        self.assertEqual(second[0]["cells"], [1, 2, 3])
        self.assertEqual(second[1], ["notice"])

    def test_cache_key_changes_with_map_parameters(self) -> None:
        base = {"seed": "0", "version": "1.21.3", "center_x": 0, "center_z": 0, "radius": 64, "size": 16}
        other = {**base, "center_x": 64}

        self.assertNotEqual(map_cache._cache_key(base), map_cache._cache_key(other))


if __name__ == "__main__":
    unittest.main()
