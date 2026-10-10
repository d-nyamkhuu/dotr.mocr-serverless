import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.batch_client import expand_sources, main


class ExpandSourcesTests(unittest.TestCase):
    def test_directory_expands_to_images_in_name_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("b.PNG", "a.jpg", "notes.txt"):
                (root / name).write_bytes(b"")
            (root / "nested").mkdir()

            sources = expand_sources([tmp, "https://example.com/page.png", "page.png"])

        self.assertEqual(
            sources,
            [str(root / "a.jpg"), str(root / "b.PNG"), "https://example.com/page.png", "page.png"],
        )


class CommandLineTests(unittest.TestCase):
    def run_main(self, argv, environ):
        stderr = io.StringIO()
        with patch.dict(os.environ, environ, clear=True), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(argv)
        return raised.exception.code, stderr.getvalue()

    def test_missing_url_and_token_are_usage_errors(self):
        code, stderr = self.run_main(["page.png"], {})
        self.assertEqual(code, 2)
        self.assertIn("DOTS_MOCR_URL", stderr)
        code, stderr = self.run_main(["page.png"], {"DOTS_MOCR_URL": "https://ocr.modal.run"})
        self.assertEqual(code, 2)
        self.assertIn("DOTS_MOCR_TOKEN", stderr)

    def test_token_is_not_a_command_line_option(self):
        code, stderr = self.run_main(["--token", "secret", "page.png"], {"DOTS_MOCR_URL": "https://ocr.modal.run"})
        self.assertEqual(code, 2)
        self.assertIn("unrecognized arguments", stderr)


if __name__ == "__main__":
    unittest.main()
