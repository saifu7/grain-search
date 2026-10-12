"""App folders (paths.py), config.json loading/saving, and the `grain config / folders / exclude`
commands. Every test points GRAIN_SEARCH_HOME at a temp folder, so the real profile is never touched."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grain_search.config import DEFAULT_EXCLUDES, Config, load_config, read_raw, save_raw  # noqa: E402
from grain_search.crawler import crawl  # noqa: E402
from grain_search.paths import app_paths  # noqa: E402


class AppPathsTest(unittest.TestCase):
    def test_windows_uses_appdata_and_localappdata(self):
        p = app_paths({"APPDATA": r"C:\U\R", "LOCALAPPDATA": r"C:\U\L"}, windows=True)
        self.assertEqual(p.config_dir, Path(r"C:\U\R") / "GrainSearch")
        self.assertEqual(p.data_dir, Path(r"C:\U\L") / "GrainSearch")
        self.assertEqual(p.config_file.name, "config.json")
        self.assertEqual(p.index_dir, p.data_dir / "index")
        self.assertEqual(p.service_file, p.data_dir / "service.json")
        self.assertEqual(p.logs_dir, p.data_dir / "logs")

    def test_windows_falls_back_to_userprofile(self):
        p = app_paths({"USERPROFILE": r"C:\Users\me"}, windows=True)
        self.assertIn("Roaming", str(p.config_dir))
        self.assertIn("Local", str(p.data_dir))

    def test_posix_follows_xdg(self):
        p = app_paths({"HOME": "/home/me"}, windows=False)
        self.assertEqual(p.config_dir, Path("/home/me/.config/grain-search"))
        self.assertEqual(p.data_dir, Path("/home/me/.local/share/grain-search"))
        p = app_paths({"HOME": "/home/me", "XDG_CONFIG_HOME": "/c", "XDG_DATA_HOME": "/d"}, windows=False)
        self.assertEqual((p.config_dir, p.data_dir), (Path("/c/grain-search"), Path("/d/grain-search")))

    def test_home_override_puts_everything_in_one_folder(self):
        for win in (True, False):
            p = app_paths({"GRAIN_SEARCH_HOME": "/x/profile", "APPDATA": "/nope"}, windows=win)
            self.assertEqual(p.config_dir, Path("/x/profile"))
            self.assertEqual(p.data_dir, Path("/x/profile"))


class ConfigFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"GRAIN_SEARCH_HOME": str(self.tmp / "home")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_settings_file_means_no_folders(self):
        cfg = load_config()
        self.assertEqual(cfg.roots, [])  # nothing built in: the user picks folders
        self.assertEqual(cfg.config_path, self.tmp / "home" / "config.json")
        self.assertEqual(cfg.data_dir, self.tmp / "home" / "index")
        self.assertEqual(cfg.service_path, self.tmp / "home" / "service.json")
        self.assertEqual(cfg.excludes, DEFAULT_EXCLUDES)

    def test_json_round_trip_and_atomic_save(self):
        path = self.tmp / "home" / "config.json"
        save_raw(path, {"roots": [str(self.tmp)], "search": {"related_limit": 5}})
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)
        self.assertFalse(list(path.parent.glob("*.tmp")))
        cfg = load_config()
        self.assertEqual(cfg.roots, [os.path.normpath(str(self.tmp))])
        self.assertEqual(cfg.related_limit, 5)

    def test_index_dir_setting_absolute_and_relative(self):
        path = self.tmp / "home" / "config.json"
        save_raw(path, {"index_dir": str(self.tmp / "elsewhere")})
        self.assertEqual(load_config().data_dir, self.tmp / "elsewhere")
        save_raw(path, {"index_dir": "idx"})  # relative to the settings file
        self.assertEqual(load_config().data_dir, (self.tmp / "home" / "idx").resolve())
        save_raw(path, {"data_dir": "old-name"})  # the older key still works
        self.assertEqual(load_config().data_dir, (self.tmp / "home" / "old-name").resolve())

    def test_yaml_still_accepted_with_explicit_path(self):
        y = self.tmp / "c.yaml"
        y.write_text("roots: ['~/x']\nmodel:\n  dim: 512\n", encoding="utf-8")
        cfg = load_config(y)
        self.assertEqual(cfg.model.dim, 512)
        self.assertEqual(cfg.config_path, y)
        with self.assertRaises(ValueError):
            save_raw(y, {})  # settings are written as JSON only

    def test_bad_settings_file_is_a_clear_error(self):
        path = self.tmp / "home" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config()

    def test_bom_is_tolerated(self):  # Notepad on Windows may save with a BOM
        path = self.tmp / "home" / "config.json"
        path.parent.mkdir(parents=True)
        path.write_bytes('\ufeff{"roots": []}'.encode())
        self.assertEqual(read_raw(path), {"roots": []})

    def test_index_folder_inside_a_root_is_never_indexed(self):
        root = self.tmp / "files"
        (root / "notes").mkdir(parents=True)
        (root / "notes" / "a.txt").write_text("hello world", encoding="utf-8")
        cfg = Config(roots=[str(root)], data_dir=root / "index")
        cfg.excludes = []
        (cfg.data_dir).mkdir()
        (cfg.data_dir / "leak.txt").write_text("index internals", encoding="utf-8")
        found = {Path(f.path).name for f in crawl(cfg)}
        self.assertIn("a.txt", found)
        self.assertNotIn("leak.txt", found)


class ExplicitFolderWinsTest(unittest.TestCase):
    """A folder the user adds is indexed even if a default exclusion covers it (e.g. AppData)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "AppData" / "Roaming" / "Notes"
        (self.root / "daily").mkdir(parents=True)
        (self.root / "node_modules").mkdir()
        (self.root / "daily" / "monday.md").write_text("standup notes", encoding="utf-8")
        (self.root / "node_modules" / "x.js").write_text("code", encoding="utf-8")
        (self.tmp / "AppData" / "Roaming" / "other.txt").write_text("not added", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def names(self, cfg):
        return {Path(f.path).name for f in crawl(cfg)}

    def test_added_folder_under_appdata_is_indexed(self):
        cfg = Config(roots=[str(self.root)], data_dir=self.tmp / "index")
        cfg.excludes = list(DEFAULT_EXCLUDES)  # includes *\\AppData\\* and *\\node_modules\\*
        found = self.names(cfg)
        self.assertIn("monday.md", found)  # the explicit folder wins over *\\AppData\\*
        self.assertNotIn("x.js", found)  # but exclusions inside it still apply

    def test_parent_root_still_respects_covering_exclusion(self):
        # its own folder name, so the test does not depend on where the temp folder lives
        (self.tmp / "skipme" / "inner").mkdir(parents=True)
        (self.tmp / "skipme" / "inner" / "secret.txt").write_text("excluded", encoding="utf-8")
        (self.tmp / "kept.txt").write_text("kept", encoding="utf-8")
        cfg = Config(roots=[str(self.tmp)], data_dir=self.tmp / "index")
        cfg.excludes = ["*/skipme/*"]
        found = self.names(cfg)
        self.assertIn("kept.txt", found)
        self.assertNotIn("secret.txt", found)  # excluded below this root, as before

    def test_live_changes_use_the_same_rule(self):
        from grain_search.crawler import excluder_for

        cfg = Config(roots=[str(self.root)], data_dir=self.tmp / "index")
        cfg.excludes = list(DEFAULT_EXCLUDES)
        ex = excluder_for(cfg)
        self.assertFalse(ex(str(self.root / "daily" / "monday.md")))
        self.assertTrue(ex(str(self.root / "node_modules" / "x.js")))
        self.assertTrue(ex(str(self.tmp / "AppData" / "Roaming" / "other.txt")))  # outside any root
        self.assertTrue(ex(str(self.tmp / "index" / "index.db")))


class CliSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.env = mock.patch.dict(os.environ, {"GRAIN_SEARCH_HOME": str(self.home)})
        self.env.start()
        (self.tmp / "docs").mkdir()
        (self.tmp / "pics").mkdir()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args) -> tuple[int, str]:
        from grain_search.cli import main

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(args))
        return code, out.getvalue()

    def test_folders_add_list_remove(self):
        code, text = self.run_cli("folders")
        self.assertEqual(code, 0)
        self.assertIn("grain folders add", text)  # hint when nothing is set up

        code, _ = self.run_cli("folders", "add", str(self.tmp / "docs"), str(self.tmp / "pics"))
        self.assertEqual(code, 0)
        self.assertEqual(len(load_config().roots), 2)

        code, text = self.run_cli("folders", "add", str(self.tmp / "docs"))
        self.assertIn("Already", text)
        self.assertEqual(len(load_config().roots), 2)

        code, text = self.run_cli("folders", "add", str(self.tmp / "missing"))
        self.assertEqual(code, 1)
        self.assertIn("Not a folder", text)

        self.run_cli("folders", "remove", str(self.tmp / "docs"))
        self.assertEqual([Path(r).name for r in load_config().roots], ["pics"])

    def test_exclude_add_folder_and_pattern_keeps_defaults(self):
        self.run_cli("exclude", "add", str(self.tmp / "docs"), "*.log")
        pats = load_config().excludes
        self.assertIn("*.log", pats)
        self.assertTrue(any(p.endswith("docs" + os.sep + "*") for p in pats))
        self.assertTrue(set(DEFAULT_EXCLUDES) <= set(pats))  # the built-in exclusions stay

        self.run_cli("exclude", "remove", "*.log")
        self.assertNotIn("*.log", load_config().excludes)

    def test_index_without_folders_explains_what_to_do(self):
        code, text = self.run_cli("--fake", "index")
        self.assertEqual(code, 1)
        self.assertIn("grain folders add", text)

    def test_broken_settings_file_gives_a_message_not_a_traceback(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text('{"roots": [', encoding="utf-8")
        code, text = self.run_cli("config")
        self.assertEqual(code, 2)
        self.assertIn("cannot read the settings file", text)

    def test_config_shows_locations(self):
        code, text = self.run_cli("config")
        self.assertEqual(code, 0)
        self.assertIn(str(self.home / "config.json"), text)
        self.assertIn("(none yet)", text)

    def test_add_folder_then_index_and_search(self):
        (self.tmp / "docs" / "note.txt").write_text("the zebra crossing is closed on Monday", encoding="utf-8")
        self.run_cli("folders", "add", str(self.tmp / "docs"))
        code, _ = self.run_cli("--fake", "index")
        self.assertEqual(code, 0)
        code, text = self.run_cli("--fake", "search", "zebra crossing")
        self.assertEqual(code, 0)
        self.assertIn("note.txt", text)


if __name__ == "__main__":
    unittest.main()
