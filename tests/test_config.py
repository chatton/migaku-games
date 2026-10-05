"""Config validation: python3 -m unittest tests.test_config (needs PyYAML; CI runs it in the image)."""
import tempfile
import unittest
from pathlib import Path

import yaml

from config import ConfigError, load_config, parse

ROOT = Path(__file__).resolve().parent.parent


def check(text):
    return parse(yaml.safe_load(text))


class ParseTest(unittest.TestCase):
    def test_shipped_config_is_valid(self):
        cfg, warnings = load_config(ROOT / "config" / "config.yaml")
        self.assertEqual(warnings, [])
        self.assertIn(cfg["active_profile"], cfg["profiles"])

    def test_empty_means_defaults(self):
        cfg, warnings = parse(None)
        self.assertEqual(cfg["retention_hours"], 24)
        self.assertIsNone(cfg["active_profile"])
        self.assertEqual(cfg["keybindings"]["translate"], "y")
        self.assertIs(cfg["copy_frame_on_card"], True)
        self.assertEqual(warnings, [])

    def test_missing_file_means_defaults_with_a_warning(self):
        cfg, warnings = load_config(Path(tempfile.gettempdir()) / "no-such-migaku-config.yaml")
        self.assertEqual(cfg["profiles"], {})
        self.assertEqual(len(warnings), 1)

    def test_profiles_fill_defaults(self):
        cfg, _ = check("active_profile: cc\nprofiles:\n  cc: {process: duckstation}\n")
        self.assertEqual(cfg["profiles"]["cc"], {"name": "cc", "freeze": False, "process": "duckstation"})

    def test_keybindings_override_and_keep_defaults(self):
        cfg, _ = check("keybindings: {translate: j}\n")
        self.assertEqual(cfg["keybindings"]["translate"], "j")
        self.assertEqual(cfg["keybindings"]["next_frame"], "]")

    def test_migaku_key_warns(self):
        _, warnings = check("keybindings: {translate: e}\n")
        self.assertTrue(any("Migaku" in w for w in warnings))

    def test_errors(self):
        bad = {
            "unknown top-level key": "colour: blue\n",
            "unknown active profile": "active_profile: nope\n",
            "negative retention": "retention_hours: -1\n",
            "bool retention": "retention_hours: true\n",
            "bad engine": "ocr_engine: tesseract\n",
            "bad profile id": "profiles:\n  'a b': {}\n",
            "unknown profile key": "profiles:\n  a: {colour: blue}\n",
            "freeze not a bool": "profiles:\n  a: {freeze: maybe}\n",
            "copy_frame_on_card not a bool": "copy_frame_on_card: yes please\n",
            "unknown action": "keybindings: {fly: f}\n",
            "duplicate key": "keybindings: {translate: v}\n",
            "not a mapping": "- a\n- b\n",
        }
        for why, text in bad.items():
            with self.subTest(why), self.assertRaises(ConfigError):
                check(text)

    def test_invalid_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            path.write_text("profiles: [unclosed\n")
            with self.assertRaises(ConfigError):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
