"""Opt-in real model smoke test; configuration is loaded only by the app.

Run from the project root:
ISLEWORKS_LIVE_TEST=1 PYTHONPATH=src python3.11 -m unittest discover -s tests/isleworks -p test_model_connection.py -v

Sends at most one request. Never prints configuration, credentials or response.
"""

import os
import unittest
import urllib.error
import urllib.request
from argparse import Namespace
from unittest.mock import patch

from isleworks.interfaces.cli import gateway


@unittest.skipUnless(os.environ.get("ISLEWORKS_LIVE_TEST") == "1",
                     "Real model test requires explicit ISLEWORKS_LIVE_TEST=1")
class ModelConnectionTest(unittest.TestCase):
    def test_configured_model_returns_json(self):
        client = None
        transport_error = None
        original_open = urllib.request.OpenerDirector.open

        def observed_open(opener, *args, **kwargs):
            nonlocal transport_error
            try:
                return original_open(opener, *args, **kwargs)
            except urllib.error.HTTPError as exc:
                transport_error = f"HTTP {exc.code}"
                raise
            except urllib.error.URLError as exc:
                transport_error = "URLError/" + type(exc.reason).__name__
                raise

        try:
            # Same loader, URL resolution and authentication as the real CLI.
            # No direct .env access, configuration output or archive writes here.
            client = gateway(Namespace(allow_paid=True, base_url=None, endpoint=None,
                                       model=None, max_calls=1), limit=1)
            client.output_tokens = 64
            client.timeout = 30
            with patch.object(urllib.request.OpenerDirector, "open", observed_open):
                response = client.request(
                    'Return only a JSON object. No explanation.',
                    'Connection test. Reply with exactly this JSON: {"ok":true}',
                )
        except Exception as exc:
            records = [] if client is None else client.records
            recorded_error = next((r.get("error") for r in reversed(records)
                                   if r.get("status") == "error"), None)
            category = transport_error or recorded_error or type(exc).__name__
            # Fixed categories only; never stringify provider errors or settings.
            raise self.failureException(
                f"Model connection failed: {category}. Configuration and response are hidden."
            ) from None

        self.assertTrue(response.get("ok") is True, "Model did not return the expected JSON marker")
        self.assertEqual(client.attempts, 1, "The smoke test must send only one request")


if __name__ == "__main__":
    unittest.main()
