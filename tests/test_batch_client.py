import tempfile
import unittest
from pathlib import Path

from scripts.batch_client import expand_sources


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


if __name__ == "__main__":
    unittest.main()
