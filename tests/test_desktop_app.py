import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

import yaml

import desktop_app


def _windows_pid_is_running(pid: int) -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return str(pid) in result.stdout


class DesktopAppHelperTests(unittest.TestCase):
    def test_video_choice_keeps_video_and_skips_image_only_note(self):
        video = desktop_app.video_choice_from_aweme(
            {
                "aweme_id": "42",
                "desc": "一个测试视频",
                "author": {"nickname": "测试作者"},
                "video": {},
            },
            "喜欢",
        )
        self.assertIsNotNone(video)
        assert video is not None
        self.assertEqual(video.url, "https://www.douyin.com/video/42")
        self.assertIsNone(desktop_app.video_choice_from_aweme({"aweme_id": "43", "desc": "图文"}, "喜欢"))

    def test_share_text_extracts_the_embedded_douyin_short_url(self):
        share_text = (
            "4.69 uFH:/ :3pm 09/18 b@a.aa 17 \u5c81\u9ad8\u4e2d\u751f\u521b\u4e1a\u505aAI\uff1a"
            "\u201c\u6211\u5df2\u7ecf\u51b3\u5b9a\u4e0d\u4e0a\u5927\u5b66\u3002\u201d "
            "https://v.douyin.com/6JeDh2x0p8M/ \u590d\u5236\u6b64\u94fe\u63a5\uff0c\u6253\u5f00\u6296\u97f3\u641c\u7d22\uff0c\u76f4\u63a5\u89c2\u770b\u89c6\u9891\uff01"
        )
        self.assertEqual(
            desktop_app.extract_douyin_url(share_text),
            "https://v.douyin.com/6JeDh2x0p8M/",
        )

    def test_transcription_progress_parser_accepts_current_item_progress(self):
        self.assertEqual(
            desktop_app.parse_transcription_progress("2/5 example title"),
            (2, 5, "example title"),
        )
        self.assertIsNone(desktop_app.parse_transcription_progress("0/5 not started"))
        self.assertIsNone(desktop_app.parse_transcription_progress("6/5 impossible"))

    def test_filter_video_choices_matches_title_author_caption_and_source(self):
        choices = [
            desktop_app.VideoChoice("1", "AI Workflow", "Wang", "local AI study", "https://www.douyin.com/video/1", "liked"),
            desktop_app.VideoChoice("2", "Kitchen tips", "Amy", "learn to fry an egg", "https://www.douyin.com/video/2", "saved cooking"),
        ]
        self.assertEqual([item.aweme_id for item in desktop_app.filter_video_choices(choices, "ai")], ["1"])
        self.assertEqual([item.aweme_id for item in desktop_app.filter_video_choices(choices, "amy")], ["2"])
        self.assertEqual([item.aweme_id for item in desktop_app.filter_video_choices(choices, "egg")], ["2"])
        self.assertEqual([item.aweme_id for item in desktop_app.filter_video_choices(choices, "saved")], ["2"])
        self.assertEqual(desktop_app.filter_video_choices(choices, "missing"), [])

    def test_filter_video_choices_ignores_spacing_and_case(self):
        choice = desktop_app.VideoChoice("1", "Agent Workflow", "author", "", "https://www.douyin.com/video/1", "liked")
        self.assertEqual(desktop_app.filter_video_choices([choice], " agentwork flow "), [choice])

    def test_downloaded_selection_ids_requires_matching_media_and_metadata(self):
        choice = desktop_app.VideoChoice("42", "title", "author", "caption", "https://www.douyin.com/video/42", "single")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            old_input = desktop_app.INPUT_DIR
            try:
                desktop_app.INPUT_DIR = root
                self.assertEqual(desktop_app.downloaded_selection_ids([choice]), set())
                (root / "video.mp4").write_bytes(b"video")
                (root / "video_data.json").write_text(
                    json.dumps({"aweme_id": "42", "desc": "title", "author": {"nickname": "author"}}),
                    encoding="utf-8",
                )
                self.assertEqual(desktop_app.downloaded_selection_ids([choice]), {"42"})
            finally:
                desktop_app.INPUT_DIR = old_input

    def test_pending_report_video_count_ignores_header_only_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            pending = Path(temporary_directory) / "pending.txt"
            pending.write_text("header only", encoding="utf-8")
            self.assertEqual(desktop_app.pending_report_video_count(pending), 0)
            pending.write_text("===== video 1 =====\ntext\n===== video 2 =====", encoding="utf-8")
            self.assertEqual(desktop_app.pending_report_video_count(pending), 2)

    def test_terminate_process_tree_stops_the_process_and_descendants(self):
        process = mock.Mock()
        process.pid = 4321
        process.poll.return_value = None
        with mock.patch.object(desktop_app.subprocess, "run") as run:
            desktop_app.terminate_process_tree(process)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command, ["taskkill", "/PID", "4321", "/T", "/F"])

    def test_run_command_reports_started_process_then_clears_it(self):
        seen = []
        code, output = desktop_app.run_command(
            [sys.executable, "-c", "print('ready')"],
            Path.cwd(),
            lambda _message: None,
            seen.append,
        )
        self.assertEqual(code, 0)
        self.assertEqual(output, "ready")
        self.assertEqual(len(seen), 2)
        self.assertIsNotNone(seen[0])
        self.assertIsNone(seen[1])

    @unittest.skipUnless(os.name == "nt", "This process-tree behavior is specific to Windows taskkill.")
    def test_terminate_process_tree_stops_real_parent_and_child(self):
        child_script = "import subprocess, sys, time; child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); print(child.pid, flush=True); time.sleep(60)"
        parent = subprocess.Popen(
            [sys.executable, "-c", child_script],
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            assert parent.stdout is not None
            child_pid = int(parent.stdout.readline().strip())
            desktop_app.terminate_process_tree(parent)
            parent.wait(timeout=8)
            deadline = time.monotonic() + 8
            while _windows_pid_is_running(child_pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(_windows_pid_is_running(child_pid))
        finally:
            desktop_app.terminate_process_tree(parent)
            if parent.stdout is not None:
                parent.stdout.close()

    def test_on_close_stops_work_and_destroys_the_window_without_a_confirmation_dialog(self):
        app = mock.Mock()
        desktop_app.DouyinDesktopApp._on_close(app)
        app._stop_current_work.assert_called_once()
        app.destroy.assert_called_once()

    def test_stop_current_work_cancels_login_and_terminates_both_processes(self):
        login_cancelled = mock.Mock()
        command_process = mock.Mock()
        login_process = mock.Mock()
        app = SimpleNamespace(
            _closing=False,
            _login_cancelled=login_cancelled,
            _process_lock=__import__("threading").Lock(),
            _command_process=command_process,
            _login_process=login_process,
        )
        with mock.patch.object(desktop_app, "terminate_process_tree") as terminate:
            desktop_app.DouyinDesktopApp._stop_current_work(app)
        self.assertTrue(app._closing)
        login_cancelled.set.assert_called_once()
        terminate.assert_has_calls([mock.call(command_process), mock.call(login_process)])

    def test_local_downloader_api_dependencies_can_be_imported(self):
        cookie_manager, api_client, url_parser = desktop_app._import_downloader()
        self.assertIsNotNone(cookie_manager)
        self.assertIsNotNone(api_client)
        self.assertIsNotNone(url_parser)

    def test_saved_login_requires_a_nonempty_local_cookie(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            vendor_root = Path(temporary_directory)
            cookie_path = vendor_root / "config" / "cookies.json"
            cookie_path.parent.mkdir()
            cookie_path.write_text("[]", encoding="utf-8")
            self.assertFalse(desktop_app.has_saved_douyin_login(vendor_root))

            cookie_path.write_text(
                json.dumps([{"name": "sessionid", "value": "local-only"}]),
                encoding="utf-8",
            )
            self.assertTrue(desktop_app.has_saved_douyin_login(vendor_root))

    def test_only_nonempty_transcripts_are_considered_completed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "state.sqlite3"
            connection = __import__("sqlite3").connect(db_path)
            connection.execute("CREATE TABLE processed_videos (aweme_id TEXT, status TEXT, transcript TEXT)")
            connection.executemany(
                "INSERT INTO processed_videos (aweme_id, status, transcript) VALUES (?, ?, ?)",
                [
                    ("download-only", "success", None),
                    ("blank-legacy", "transcribed", ""),
                    ("already-transcribed", "transcribed", "actual text"),
                ],
            )
            connection.commit()
            connection.close()
            self.assertEqual(desktop_app.completed_video_ids(db_path), {"already-transcribed"})

    def test_selection_batch_uses_an_isolated_input_folder_and_no_cookies(self):
        choice = desktop_app.VideoChoice("42", "标题", "作者", "文案", "https://www.douyin.com/video/42", "喜欢")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            old_runtime = desktop_app.RUNTIME_DIR
            old_queue = desktop_app.SELECTION_FILE
            old_download_config = desktop_app.DOWNLOAD_CONFIG
            old_process_config = desktop_app.PROCESS_CONFIG
            old_batch_root = desktop_app.BATCH_ROOT
            try:
                desktop_app.RUNTIME_DIR = root
                desktop_app.SELECTION_FILE = root / "selected.json"
                desktop_app.DOWNLOAD_CONFIG = root / "selected-download.yml"
                desktop_app.PROCESS_CONFIG = root / "selected-process.yml"
                desktop_app.BATCH_ROOT = root / "batches"
                batch = desktop_app.write_selection_files([choice])
                self.assertTrue(batch.input_dir.is_dir())
                self.assertNotEqual(batch.input_dir, desktop_app.INPUT_DIR)
                self.assertIn('"aweme_id": "42"', batch.queue_path.read_text(encoding="utf-8"))
                config = yaml.safe_load(batch.download_config_path.read_text(encoding="utf-8"))
                self.assertEqual(config["link"], [choice.url])
                self.assertEqual(Path(config["path"]), batch.input_dir)
                self.assertEqual(config["mode"], [])
                self.assertFalse(config["folderstyle"])
                self.assertFalse(config["group_by_mode"])
                self.assertEqual(config["filename_template"], "{id}")
                self.assertEqual(config["video_quality"], "540p")
                self.assertFalse(config["database"])
                self.assertNotIn("cookies", config)
                self.assertTrue(config["auto_cookie"])
                process_config = yaml.safe_load(batch.process_config_path.read_text(encoding="utf-8"))
                self.assertEqual(Path(process_config["input_dir"]), batch.input_dir)
            finally:
                desktop_app.RUNTIME_DIR = old_runtime
                desktop_app.SELECTION_FILE = old_queue
                desktop_app.DOWNLOAD_CONFIG = old_download_config
                desktop_app.PROCESS_CONFIG = old_process_config
                desktop_app.BATCH_ROOT = old_batch_root

    def test_repairs_missing_batch_metadata_for_downloaded_media(self):
        choice = desktop_app.VideoChoice(
            "12345", "title", "author", "caption", "https://www.douyin.com/video/12345", "like"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            batch = Path(temporary_directory)
            media = batch / "12345.mp4"
            media.write_bytes(b"media")
            self.assertEqual(desktop_app.downloaded_selection_ids([choice], batch), set())

            repaired = desktop_app.repair_missing_batch_metadata([choice], batch)

            self.assertEqual(repaired, 1)
            metadata = batch / "12345_data.json"
            self.assertTrue(metadata.exists())
            data = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual(data["aweme_id"], "12345")
            self.assertEqual(data["desc"], "title")
            self.assertEqual(desktop_app.downloaded_selection_ids([choice], batch), {"12345"})

    def test_repairs_missing_metadata_when_the_video_is_stored_inside_an_id_folder(self):
        choice = desktop_app.VideoChoice(
            "67890", "title", "author", "caption", "https://www.douyin.com/video/67890", "like"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            batch = Path(temporary_directory)
            media_folder = batch / "67890"
            media_folder.mkdir()
            media = media_folder / "downloaded.mp4"
            media.write_bytes(b"media")

            repaired = desktop_app.repair_missing_batch_metadata([choice], batch)

            self.assertEqual(repaired, 1)
            self.assertTrue((media_folder / "downloaded_data.json").exists())
            self.assertEqual(desktop_app.downloaded_selection_ids([choice], batch), {"67890"})

    def test_login_command_uses_the_existing_local_cookie_capture_tool(self):
        command = desktop_app.command_for_login()
        self.assertEqual(command[:6], [
            "uv", "run", "--project", ".", "--extra", "browser",
        ])
        self.assertIn("tools/cookie_fetcher.py", command)
        self.assertIn("config.local.yml", command)

    def test_cleanup_completed_batch_keeps_failed_media_and_removes_success_only(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            empty_batch = root / "empty"
            empty_batch.mkdir()
            (empty_batch / "download_manifest.jsonl").write_text("{}", encoding="utf-8")
            self.assertTrue(desktop_app.cleanup_completed_batch(empty_batch))
            self.assertFalse(empty_batch.exists())

            failed_batch = root / "failed"
            failed_batch.mkdir()
            (failed_batch / "video.mp4").write_bytes(b"media")
            self.assertFalse(desktop_app.cleanup_completed_batch(failed_batch))
            self.assertTrue(failed_batch.exists())



if __name__ == "__main__":
    unittest.main()
