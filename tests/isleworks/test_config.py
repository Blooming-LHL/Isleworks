import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from isleworks.config import deepseek_settings, resolve_chat_endpoint
from isleworks.interfaces.cli import gateway


class ConfigTest(unittest.TestCase):
    def write(self, contents: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / ".env"
        path.write_text(contents, encoding="utf-8")
        return path

    def test_quoted_values_comments_and_export(self):
        path = self.write(
            '\ufeff# comment\nexport DEEPSEEK_API_KEY="quoted key"\n'
            "DEEPSEEK_BASE_URL='https://example.test/v1'\nDEEPSEEK_MODEL=flash\n"
        )
        self.assertEqual(
            deepseek_settings(path, environ={}),
            {
                "DEEPSEEK_API_KEY": "quoted key",
                "DEEPSEEK_BASE_URL": "https://example.test/v1",
                "DEEPSEEK_MODEL": "flash",
            },
        )

    def test_values_are_not_expanded(self):
        path = self.write("DEEPSEEK_MODEL=$OTHER_MODEL\n")
        self.assertEqual(deepseek_settings(path, environ={})["DEEPSEEK_MODEL"], "$OTHER_MODEL")

    def test_environment_precedence_including_empty(self):
        path = self.write("DEEPSEEK_API_KEY=file\nDEEPSEEK_MODEL=file-model\n")
        self.assertEqual(
            deepseek_settings(path, environ={"DEEPSEEK_API_KEY": "", "DEEPSEEK_MODEL": "env-model"}),
            {
                "DEEPSEEK_API_KEY": "",
                "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "DEEPSEEK_MODEL": "env-model",
            },
        )

    def test_missing_file_defaults(self):
        self.assertEqual(
            deepseek_settings("/definitely/missing/isleworks.env", environ={}),
            {
                "DEEPSEEK_API_KEY": "",
                "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                "DEEPSEEK_MODEL": "deepseek-flash",
            },
        )

    def test_malformed_recognized_line_is_sanitized(self):
        path = self.write("DEEPSEEK_MODEL='unclosed\n")
        with self.assertRaisesRegex(ValueError, r"^Malformed configuration line 1$"):
            deepseek_settings(path, environ={})

    def test_does_not_mutate_environment(self):
        before = os.environ.copy()
        deepseek_settings(self.write("DEEPSEEK_MODEL=local\n"), environ={})
        self.assertEqual(dict(os.environ), before)

    def test_bare_export_and_unrelated_settings_are_ignored(self):
        settings = deepseek_settings(self.write("export\nOTHER_SETTING=ignored\n"), environ={})
        self.assertEqual(settings["DEEPSEEK_MODEL"], "deepseek-flash")

    def test_resolve_chat_endpoint_variants_and_validation(self):
        self.assertEqual(resolve_chat_endpoint("https://api.deepseek.com"), "https://api.deepseek.com/chat/completions")
        self.assertEqual(resolve_chat_endpoint("https://api.deepseek.com/"), "https://api.deepseek.com/chat/completions")
        self.assertEqual(resolve_chat_endpoint("https://api.deepseek.com/v1/"), "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(resolve_chat_endpoint("https://api.deepseek.com/chat/completions"), "https://api.deepseek.com/chat/completions")
        for value in ("http://example.test", "https://user:pass@example.test", "https://example.test?x=1", "https://example.test#x"):
            with self.assertRaises(ValueError):
                resolve_chat_endpoint(value)

    def test_legacy_endpoint_environment_and_file_precedence(self):
        path = self.write("DEEPSEEK_ENDPOINT=https://file.example\n")
        self.assertEqual(deepseek_settings(path, environ={})["DEEPSEEK_BASE_URL"], "https://file.example")
        self.assertEqual(deepseek_settings(path, environ={"DEEPSEEK_ENDPOINT": "https://env.example"})["DEEPSEEK_BASE_URL"], "https://env.example")
        self.assertEqual(deepseek_settings(path, environ={"DEEPSEEK_BASE_URL": "https://canonical.example", "DEEPSEEK_ENDPOINT": "https://legacy.example"})["DEEPSEEK_BASE_URL"], "https://canonical.example")

    def test_gateway_settings_and_cli_overrides_without_requests(self):
        settings = {"DEEPSEEK_API_KEY": "test-only", "DEEPSEEK_BASE_URL": "https://example.test/v1",
                    "DEEPSEEK_MODEL": "configured-model"}
        args = Namespace(allow_paid=True, base_url=None, endpoint=None, model=None, max_calls=12)
        with patch("isleworks.interfaces.cli.deepseek_settings", return_value=settings):
            configured = gateway(args)
            self.assertEqual((configured.endpoint, configured.model, configured.max_calls),
                             ("https://example.test/v1/chat/completions", "configured-model", 12))
            args.endpoint, args.model = "https://override.test/chat/completions", "cli-model"
            overridden = gateway(args)
            self.assertEqual((overridden.endpoint, overridden.model),
                             ("https://override.test/chat/completions", "cli-model"))
            self.assertEqual(configured.attempts + overridden.attempts, 0)

    def test_legacy_environment_overrides_canonical_file_setting(self):
        path = self.write("DEEPSEEK_BASE_URL=https://file.example\n")
        self.assertEqual(
            deepseek_settings(path, environ={"DEEPSEEK_ENDPOINT": "https://env.example"})["DEEPSEEK_BASE_URL"],
            "https://env.example",
        )

    def test_gateway_paid_guard_precedes_configuration_read(self):
        with patch("isleworks.interfaces.cli.deepseek_settings") as settings:
            with self.assertRaises(ValueError):
                gateway(Namespace(allow_paid=False))
            settings.assert_not_called()


if __name__ == "__main__":
    unittest.main()
