import sqlite3
import tempfile
import unittest
from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("process_likes", ROOT / "process_likes.py")
process_likes = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = process_likes
SPEC.loader.exec_module(process_likes)


class CodexPendingTranscriptTests(unittest.TestCase):
    def test_pending_transcript_is_saved_and_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = process_likes.create_database(root / "state.sqlite3")
            item = process_likes.VideoItem(
                root / "video.mp4", root / "video_data.json", "123", "测试标题", "测试作者", "https://www.douyin.com/video/123"
            )
            process_likes.save_transcript_pending(connection, item, "这是仅保存在本机的转写文字。")
            pending = root / "待Codex总结.txt"
            self.assertEqual(process_likes.write_pending_report(connection, pending), 1)
            exported = pending.read_text(encoding="utf-8-sig")
            self.assertIn("测试标题", exported)
            self.assertIn("https://www.douyin.com/video/123", exported)
            self.assertIn("仅保存在本机的转写文字", exported)
            status, transcript = connection.execute("SELECT status, transcript FROM processed_videos WHERE aweme_id='123'").fetchone()
            self.assertEqual(status, "transcribed")
            self.assertEqual(transcript, "这是仅保存在本机的转写文字。")
            connection.close()

    def test_pending_report_can_be_limited_to_the_current_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = process_likes.create_database(root / "state.sqlite3")
            try:
                first = process_likes.VideoItem(root / "a.mp4", root / "a_data.json", "a", "current-batch", "author-a", "https://www.douyin.com/video/a")
                older = process_likes.VideoItem(root / "b.mp4", root / "b_data.json", "b", "older-batch", "author-b", "https://www.douyin.com/video/b")
                process_likes.save_transcript_pending(connection, first, "current transcript")
                process_likes.save_transcript_pending(connection, older, "older transcript")
                pending = root / "pending.txt"
                self.assertEqual(process_likes.write_pending_report(connection, pending, {"a"}), 1)
                exported = pending.read_text(encoding="utf-8-sig")
                self.assertIn("current-batch", exported)
                self.assertIn("current transcript", exported)
                self.assertNotIn("older-batch", exported)
                self.assertNotIn("older transcript", exported)
            finally:
                connection.close()

    def test_pending_report_excludes_legacy_blank_transcript_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = process_likes.create_database(root / "state.sqlite3")
            try:
                blank = process_likes.VideoItem(root / "blank.mp4", root / "blank_data.json", "blank", "old blank", "author", "https://www.douyin.com/video/blank")
                usable = process_likes.VideoItem(root / "usable.mp4", root / "usable_data.json", "usable", "usable text", "author", "https://www.douyin.com/video/usable")
                connection.execute(
                    "INSERT INTO processed_videos (aweme_id, title, author, url, status, processed_at, transcript) VALUES (?, ?, ?, ?, 'transcribed', '2026-07-13T00:00:00', '')",
                    (blank.aweme_id, blank.title, blank.author, blank.url),
                )
                connection.commit()
                process_likes.save_transcript_pending(connection, usable, "nonempty transcript")
                pending = root / "pending.txt"
                self.assertEqual(process_likes.write_pending_report(connection, pending), 1)
                exported = pending.read_text(encoding="utf-8-sig")
                self.assertIn("usable text", exported)
                self.assertNotIn("old blank", exported)
            finally:
                connection.close()

    def test_existing_database_is_migrated_with_transcript_column(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "legacy.sqlite3"
            legacy_connection = sqlite3.connect(db)
            try:
                legacy_connection.execute("CREATE TABLE processed_videos (aweme_id TEXT PRIMARY KEY, title TEXT, author TEXT, url TEXT, summary TEXT, status TEXT, error TEXT, processed_at TEXT)")
                legacy_connection.commit()
            finally:
                legacy_connection.close()
            connection = process_likes.create_database(db)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(processed_videos)")}
            self.assertIn("transcript", columns)
            connection.close()


    def test_empty_audio_is_exported_with_a_clear_placeholder(self):
        class EmptySpeechModel:
            def transcribe(self, *_args, **_kwargs):
                return [], None

        transcript = process_likes.transcribe(EmptySpeechModel(), Path("silent.mp4"), "zh")

        self.assertEqual(transcript, "[\u672a\u68c0\u6d4b\u5230\u53ef\u8bc6\u522b\u8bed\u97f3\uff1a\u89c6\u9891\u53ef\u80fd\u6ca1\u6709\u4eba\u58f0\u3001\u97f3\u91cf\u592a\u4f4e\u6216\u80cc\u666f\u97f3\u592a\u5f3a\u3002]")


if __name__ == "__main__":
    unittest.main()
