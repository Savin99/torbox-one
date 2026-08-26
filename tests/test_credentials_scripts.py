#!/usr/bin/env python3
"""Tests for the interactive credential configuration helper."""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
CONFIGURE_SCRIPT = PROJECT_DIR / "scripts" / "configure-credentials.sh"
CONFIGURE_CATALOG_SCRIPT = PROJECT_DIR / "scripts" / "configure-catalog.sh"
SYNC_PROWLARR_SCRIPT = PROJECT_DIR / "scripts" / "sync-prowlarr.sh"


class ConfigureCredentialsTests(unittest.TestCase):
    def test_creates_private_env_without_echoing_secrets(self) -> None:
        telegram = "123456:abcdefghijklmnopqrstuvwxyz"
        torbox = "torbox-secret-key-1234567890"

        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            result = subprocess.run(
                [str(CONFIGURE_SCRIPT), "--output", str(env_file)],
                input=f"{telegram}\n{torbox}\n123456789\n",
                text=True,
                capture_output=True,
                check=True,
            )

            contents = env_file.read_text(encoding="utf-8")
            self.assertIn(f"TELEGRAM_BOT_TOKEN={telegram}", contents)
            self.assertIn(f"TORBOX_API_KEY={torbox}", contents)
            self.assertIn("OWNER_TELEGRAM_ID=123456789", contents)
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
            self.assertNotIn(telegram, result.stdout + result.stderr)
            self.assertNotIn(torbox, result.stdout + result.stderr)

    def test_blank_input_keeps_existing_values(self) -> None:
        telegram = "654321:zyxwvutsrqponmlkjihgfedcba"
        torbox = "existing-torbox-secret-1234"

        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text(
                f"TELEGRAM_BOT_TOKEN={telegram}\n"
                f"TORBOX_API_KEY={torbox}\n"
                "OWNER_TELEGRAM_ID=987654321\n"
                "LOG_LEVEL=DEBUG\n",
                encoding="utf-8",
            )
            subprocess.run(
                [str(CONFIGURE_SCRIPT), "--output", str(env_file)],
                input="\n\n\n",
                text=True,
                capture_output=True,
                check=True,
            )

            contents = env_file.read_text(encoding="utf-8")
            self.assertIn(f"TELEGRAM_BOT_TOKEN={telegram}", contents)
            self.assertIn(f"TORBOX_API_KEY={torbox}", contents)
            self.assertIn("OWNER_TELEGRAM_ID=987654321", contents)
            self.assertIn("LOG_LEVEL=DEBUG", contents)


class SyncProwlarrTests(unittest.TestCase):
    def test_updates_private_env_without_printing_api_key(self) -> None:
        api_key = "prowlarr-secret-api-key-123456"

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env_file = root / ".env"
            config_file = root / "config.xml"
            env_file.write_text(
                "TELEGRAM_BOT_TOKEN=keep\n"
                "PROWLARR_URL=\n"
                "PROWLARR_API_KEY=\n",
                encoding="utf-8",
            )
            config_file.write_text(
                f"<Config><ApiKey>{api_key}</ApiKey></Config>",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update({
                "BOT_ENV_FILE": str(env_file),
                "PROWLARR_CONFIG_FILE": str(config_file),
                "PROWLARR_ADDRESS": "http://127.0.0.1:19696",
            })

            result = subprocess.run(
                [str(SYNC_PROWLARR_SCRIPT)],
                env=environment,
                text=True,
                capture_output=True,
                check=True,
            )

            contents = env_file.read_text(encoding="utf-8")
            self.assertIn("TELEGRAM_BOT_TOKEN=keep", contents)
            self.assertIn("PROWLARR_URL=http://127.0.0.1:19696", contents)
            self.assertIn(f"PROWLARR_API_KEY={api_key}", contents)
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
            self.assertNotIn(api_key, result.stdout + result.stderr)


class ConfigureCatalogTests(unittest.TestCase):
    def test_saves_catalog_key_without_echoing_it(self) -> None:
        api_key = "catalog-secret-api-key-123456"
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text("LOG_LEVEL=INFO\nKINOPOISK_API_KEY=\n", encoding="utf-8")
            result = subprocess.run(
                [str(CONFIGURE_CATALOG_SCRIPT), "--output", str(env_file)],
                input=f"{api_key}\n",
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertIn(f"KINOPOISK_API_KEY={api_key}", env_file.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
            self.assertNotIn(api_key, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
