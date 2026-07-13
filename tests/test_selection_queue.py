import json
import tempfile
import unittest
from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("process_likes_selection", ROOT / "process_likes.py")
process_likes = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = process_likes
SPEC.loader.exec_module(process_likes)


class SelectedQueueTests(unittest.TestCase):
    def test_selected_queue_limits_items_to_selected_ids(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "selected.json"
            queue_path.write_text(json.dumps([{"aweme_id": "keep"}]), encoding="utf-8")
            allowed = process_likes.load_allowed_ids(queue_path)
            self.assertEqual(allowed, {"keep"})


if __name__ == "__main__":
    unittest.main()
