from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
import tempfile

if "patchright" not in sys.modules and importlib.util.find_spec("patchright.async_api") is None:
    patchright_mod = types.ModuleType("patchright")
    async_api_mod = types.ModuleType("patchright.async_api")
    async_api_mod.Page = object
    async_api_mod.BrowserContext = object
    async_api_mod.Playwright = object
    async_api_mod.Frame = object
    async_api_mod.Request = object
    async_api_mod.Response = object
    async_api_mod.async_playwright = lambda: None
    sys.modules["patchright"] = patchright_mod
    sys.modules["patchright.async_api"] = async_api_mod

from src.browser.manager import _cleanup_stale_locks, _matching_browser_processes


class BrowserCleanupTests(unittest.TestCase):
    def test_matching_browser_processes_targets_profile_only(self) -> None:
        profile = Path("/Users/test/catgpt/browser_data")
        ps_out = """
        10 1 /usr/bin/python bash
        101 1 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/tmp/other-profile --type=renderer
        102 1 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/Users/test/catgpt/browser_data
        103 102 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/Users/test/catgpt/browser_data --type=renderer
        104 1 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir=/Users/test/catgpt/browser_data2
        105 1 /Applications/Google Chrome for Testing.app/Contents/MacOS/chrome-for-testing --user-data-dir=/Users/test/catgpt/browser_data
        """
        matches = _matching_browser_processes(profile, ps_out)
        self.assertEqual(matches, {102, 103, 105})

    def test_cleanup_keeps_profile_state_while_removing_only_stale_lock_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir)
            (profile / "SingletonLock").write_text("stale", encoding="utf-8")
            (profile / "Default").mkdir()
            (profile / "Default" / "Network Persistent State").write_text("network", encoding="utf-8")
            (profile / "Default" / "Cache").mkdir()
            (profile / "Default" / "Cache" / "dummy").write_text("cache", encoding="utf-8")
            _cleanup_stale_locks(profile)
            self.assertFalse((profile / "SingletonLock").exists())
            self.assertTrue((profile / "Default" / "Network Persistent State").exists())
            self.assertTrue((profile / "Default" / "Cache").exists())


if __name__ == "__main__":
    unittest.main()
