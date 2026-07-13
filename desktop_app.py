"""Desktop picker for local, private Douyin video transcription.

The app lists a user's liked videos or saved-collection videos first.  It only
passes the rows chosen in the table to the existing local downloader and
faster-whisper transcription workflow.  No video, audio, cookie, or account
information is uploaded by this app.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app_paths import ROOT, VENDOR_ROOT

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import yaml

# sv-ttk is installed from PyPI for new installs.  The local fallback keeps
# older unpacked copies working until they are migrated.
THEME_LIBRARY_ROOT = ROOT / "external" / "Sun-Valley-ttk-theme"
try:
    import sv_ttk
except ImportError:  # pragma: no cover - legacy local fallback
    if THEME_LIBRARY_ROOT.exists() and str(THEME_LIBRARY_ROOT) not in sys.path:
        sys.path.insert(0, str(THEME_LIBRARY_ROOT))
    try:
        import sv_ttk
    except ImportError:
        sv_ttk = None
OUTPUT_DIR = ROOT / "output"
RUNTIME_DIR = ROOT / "runtime"
STATE_DB = OUTPUT_DIR / "processing_state.sqlite3"
PENDING_TXT = OUTPUT_DIR / "待Codex总结.txt"
LOCAL_CONFIG = ROOT / "config.yml"
CONFIG_EXAMPLE = ROOT / "config.example.yml"
SELECTION_FILE = RUNTIME_DIR / "selected_videos.json"
DOWNLOAD_CONFIG = RUNTIME_DIR / "selected-download.yml"
PROCESS_CONFIG = RUNTIME_DIR / "selected-process.yml"
BATCH_ROOT = RUNTIME_DIR / "batches"
INPUT_DIR = ROOT / "input"
PROGRESS_PHASE_PREFIX = "__douyin_phase__:"
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-_][0-?]*[ -/]*[@-~]|\[[0-?]*[ -/]*[@-~])")


@dataclass(frozen=True)
class VideoChoice:
    aweme_id: str
    title: str
    author: str
    caption: str
    url: str
    source: str


@dataclass(frozen=True)
class BatchJob:
    """Private, disposable files for one desktop processing run."""

    queue_path: Path
    download_config_path: Path
    process_config_path: Path
    input_dir: Path


def _import_downloader() -> tuple[Any, Any, Any]:
    """Import the vendored downloader without requiring a separate install."""
    vendor = str(VENDOR_ROOT)
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    from auth import CookieManager
    from core.api_client import DouyinAPIClient
    from core.url_parser import URLParser

    return CookieManager, DouyinAPIClient, URLParser


def has_saved_douyin_login(vendor_root: Path = VENDOR_ROOT) -> bool:
    """Return whether a non-empty local login cookie file is available.

    This deliberately checks only local file structure, never displays or sends
    cookie values. A real request may still fail if Douyin has expired the login.
    """
    cookie_path = vendor_root / "config" / "cookies.json"
    try:
        data = json.loads(cookie_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if isinstance(data, list):
        return any(isinstance(item, dict) and item.get("name") and item.get("value") for item in data)
    if isinstance(data, dict):
        return bool(data)
    return False


def completed_video_ids(db_path: Path = STATE_DB) -> set[str]:
    if not db_path.exists():
        return set()
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT aweme_id
            FROM processed_videos
            WHERE status = 'transcribed'
              AND transcript IS NOT NULL
              AND TRIM(transcript) <> ''
            """
        ).fetchall()
        return {str(row[0]) for row in rows if row[0]}
    except sqlite3.DatabaseError:
        return set()
    finally:
        connection.close()



def downloaded_selection_ids(
    choices: list[VideoChoice], input_dir: Path | None = None
) -> set[str]:
    """Return selected IDs that have both media and metadata in this batch only."""
    input_dir = input_dir or INPUT_DIR
    selected_ids = {choice.aweme_id for choice in choices if choice.aweme_id}
    if not selected_ids:
        return set()
    from process_likes import find_items

    return {item.aweme_id for item in find_items(input_dir, set(), selected_ids)}


def repair_missing_batch_metadata(choices: list[VideoChoice], input_dir: Path) -> int:
    """Create minimal local metadata only when the downloader saved media without it.

    The downloadable file is never invented or moved.  This narrow recovery path
    merely makes an already-downloaded, selected media file visible to the local
    transcription step when a downloader response omitted its ``*_data.json``.
    """
    from process_likes import VIDEO_SUFFIXES

    already_ready = downloaded_selection_ids(choices, input_dir)
    repaired = 0
    for choice in choices:
        if not choice.aweme_id or choice.aweme_id in already_ready:
            continue
        candidates = sorted(
            path
            for path in input_dir.rglob("*")
            if path.is_file()
            and path.suffix.lower() in VIDEO_SUFFIXES
            # Most batch downloads use the ID in the filename. Some downloader
            # versions instead store a generic filename inside an ID-named folder.
            # Both layouts identify the selected video without guessing based on
            # a title or unrelated media in the same temporary batch.
            and (
                choice.aweme_id in path.stem
                or choice.aweme_id in {parent.name for parent in path.parents}
            )
        )
        if not candidates:
            continue
        media_path = candidates[0]
        metadata_path = media_path.with_name(f"{media_path.stem}_data.json")
        if metadata_path.exists():
            continue
        metadata = {
            "aweme_id": choice.aweme_id,
            "desc": choice.title,
            "author": {"nickname": choice.author},
            "share_url": choice.url,
            "source": choice.source,
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        repaired += 1
    return repaired


def pending_report_video_count(pending_path: Path = PENDING_TXT) -> int:
    """Count transcribed video entries, not just the header of a pending TXT."""
    if not pending_path.exists():
        return 0
    try:
        text = pending_path.read_text(encoding="utf-8-sig")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if line.startswith("====="))


def _as_text(value: Any) -> str:
    return str(value or "").replace("\n", " ").strip()


def _search_key(value: str) -> str:
    """Normalize text for the optional local list filter."""
    return re.sub(r"\s+", "", _as_text(value)).casefold()


def filter_video_choices(choices: list[VideoChoice], query: str) -> list[VideoChoice]:
    """Return choices matching the local title, author, source, or caption filter."""
    needle = _search_key(query)
    if not needle:
        return list(choices)
    return [
        choice
        for choice in choices
        if needle in _search_key(
            " ".join((choice.title, choice.author, choice.caption, choice.source, choice.url, choice.aweme_id))
        )
    ]


DOUYIN_URL_PATTERN = re.compile(
    r"((?:https?://)?(?:v\.douyin\.com|v\.iesdouyin\.com|www\.douyin\.com|douyin\.com)/[^\s<>\"']+)",
    re.IGNORECASE,
)


def parse_transcription_progress(message: str) -> tuple[int, int, str] | None:
    """Extract an ``N/total`` progress item emitted by the local transcriber."""
    match = re.search(r"\b(\d+)\s*/\s*(\d+)\b\s*(.*)$", message)
    if not match:
        return None
    current = int(match.group(1))
    total = int(match.group(2))
    if current < 1 or total < 1 or current > total:
        return None
    return current, total, match.group(3).strip()


def extract_douyin_url(share_text: str) -> str | None:
    """Extract the usable URL from Douyin's full share-message text."""
    match = DOUYIN_URL_PATTERN.search(share_text or "")
    if not match:
        return None
    url = match.group(1).rstrip("\u3002\uff0c\u3001\uff01\uff1f\uff1a\uff1b\uff09\uff3d\u3011\uff5d>)]}\"'.,;:!?")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def video_choice_from_aweme(item: dict[str, Any], source: str) -> VideoChoice | None:
    """Convert a Douyin API entry to a compact, display-safe selectable row."""
    aweme_id = _as_text(item.get("aweme_id") or item.get("aweme_id_str"))
    if not aweme_id:
        return None

    # The local workflow transcribes spoken audio.  Do not offer image-only
    # notes because they have no video stream for faster-whisper to process.
    video = item.get("video")
    if not isinstance(video, dict) and not item.get("video_play_addr"):
        return None

    author_data = item.get("author") if isinstance(item.get("author"), dict) else {}
    title = _as_text(item.get("desc")) or f"抖音视频 {aweme_id}"
    author = _as_text(author_data.get("nickname")) or "未知作者"
    return VideoChoice(
        aweme_id=aweme_id,
        title=title,
        author=author,
        caption=title,
        url=f"https://www.douyin.com/video/{aweme_id}",
        source=source,
    )


def _extract_folder_id(item: dict[str, Any]) -> str:
    info = item.get("collects_info") if isinstance(item.get("collects_info"), dict) else {}
    return _as_text(
        item.get("collects_id")
        or item.get("collects_id_str")
        or item.get("id")
        or info.get("collects_id")
        or info.get("collects_id_str")
    )


def _extract_folder_name(item: dict[str, Any]) -> str:
    info = item.get("collects_info") if isinstance(item.get("collects_info"), dict) else {}
    return _as_text(
        item.get("collects_name")
        or item.get("name")
        or item.get("title")
        or info.get("collects_name")
        or info.get("name")
    ) or "未命名收藏夹"


async def _open_api_client() -> tuple[Any, Any, Any, str]:
    """Return a logged-in client and the current account's sec_uid."""
    CookieManager, DouyinAPIClient, URLParser = _import_downloader()
    cookie_path = VENDOR_ROOT / "config" / "cookies.json"
    cookies = CookieManager(str(cookie_path)).get_cookies()
    if not cookies:
        raise RuntimeError("没有找到本机抖音登录信息。请先完成一次本地抖音登录。")
    client = DouyinAPIClient(cookies)
    await client.__aenter__()
    try:
        user = await client.get_self_info()
        sec_uid = _as_text((user or {}).get("sec_uid"))
        if not sec_uid:
            raise RuntimeError("无法确认当前抖音登录账号，请重新登录后再试。")
        return client, CookieManager, URLParser, sec_uid
    except Exception:
        await client.__aexit__(None, None, None)
        raise


async def fetch_liked_videos(
    limit: int, excluded_ids: set[str], progress: Callable[[str], None]
) -> list[VideoChoice]:
    client, _, _, sec_uid = await _open_api_client()
    try:
        collected: list[VideoChoice] = []
        seen: set[str] = set()
        cursor = 0
        has_more = True
        while has_more and len(collected) < limit:
            progress(f"正在读取喜欢列表：已找到 {len(collected)} / {limit} 条可选视频…")
            page = await client.get_user_like(sec_uid, max_cursor=cursor, count=20)
            items = page.get("items") or []
            if not items:
                break
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                choice = video_choice_from_aweme(raw, "喜欢")
                if not choice or choice.aweme_id in seen or choice.aweme_id in excluded_ids:
                    continue
                seen.add(choice.aweme_id)
                collected.append(choice)
                if len(collected) >= limit:
                    break
            has_more = bool(page.get("has_more", False))
            next_cursor = int(page.get("max_cursor", 0) or 0)
            if has_more and next_cursor == cursor:
                break
            cursor = next_cursor
            await asyncio.sleep(0.12)
        return collected
    finally:
        await client.__aexit__(None, None, None)


async def fetch_collected_videos(
    limit: int, excluded_ids: set[str], progress: Callable[[str], None]
) -> list[VideoChoice]:
    client, _, _, _ = await _open_api_client()
    try:
        folders: list[dict[str, Any]] = []
        cursor = 0
        has_more = True
        while has_more:
            page = await client.get_user_collects("self", max_cursor=cursor, count=10)
            items = page.get("items") or []
            if not items:
                break
            folders.extend(item for item in items if isinstance(item, dict))
            has_more = bool(page.get("has_more", False))
            next_cursor = int(page.get("max_cursor", 0) or 0)
            if has_more and next_cursor == cursor:
                break
            cursor = next_cursor
            await asyncio.sleep(0.12)

        collected: list[VideoChoice] = []
        seen: set[str] = set()
        for folder_index, folder in enumerate(folders, start=1):
            if len(collected) >= limit:
                break
            folder_id = _extract_folder_id(folder)
            if not folder_id:
                continue
            folder_name = _extract_folder_name(folder)
            page_cursor = 0
            page_more = True
            while page_more and len(collected) < limit:
                progress(
                    f"正在读取收藏夹 {folder_index}/{len(folders)}："
                    f"{folder_name}（已找到 {len(collected)} / {limit} 条）…"
                )
                page = await client.get_collect_aweme(folder_id, max_cursor=page_cursor, count=20)
                items = page.get("items") or []
                if not items:
                    break
                for raw in items:
                    if not isinstance(raw, dict):
                        continue
                    choice = video_choice_from_aweme(raw, f"收藏 · {folder_name}")
                    if not choice or choice.aweme_id in seen or choice.aweme_id in excluded_ids:
                        continue
                    seen.add(choice.aweme_id)
                    collected.append(choice)
                    if len(collected) >= limit:
                        break
                page_more = bool(page.get("has_more", False))
                next_cursor = int(page.get("max_cursor", 0) or 0)
                if page_more and next_cursor == page_cursor:
                    break
                page_cursor = next_cursor
                await asyncio.sleep(0.12)
        return collected
    finally:
        await client.__aexit__(None, None, None)


async def fetch_single_video(url: str, excluded_ids: set[str]) -> VideoChoice:
    client, _, URLParser, _ = await _open_api_client()
    try:
        raw_url = extract_douyin_url(url)
        if not raw_url:
            raise ValueError("这不是可识别的抖音视频链接。请粘贴视频的分享链接。")
        if "v.douyin.com" in raw_url or "v.iesdouyin.com" in raw_url:
            resolved = await client.resolve_short_url(raw_url)
            if not resolved:
                raise RuntimeError("短链接解析失败，请从抖音重新复制完整链接后再试。")
            raw_url = resolved
        parsed = URLParser.parse(raw_url)
        if not parsed or not parsed.get("aweme_id"):
            raise ValueError("这不是可识别的抖音视频链接。请粘贴视频的分享链接。")
        detail = await client.get_video_detail(str(parsed["aweme_id"]))
        if not isinstance(detail, dict):
            raise RuntimeError("没有获取到视频信息，可能链接已失效或登录已过期。")
        choice = video_choice_from_aweme(detail, "单个链接")
        if not choice:
            raise RuntimeError("该链接不是可转写的视频，或视频信息不完整。")
        if choice.aweme_id in excluded_ids:
            raise RuntimeError("这个视频已经转写或总结过，因此没有再次加入。")
        return choice
    finally:
        await client.__aexit__(None, None, None)


def write_selection_files(choices: list[VideoChoice]) -> BatchJob:
    """Create an isolated temporary download folder for one selected batch.

    The downloader treats any media already under its output path as complete.
    Reusing the old shared ``input`` directory therefore made it skip an old
    orphaned file and left the desktop app with no matching metadata to
    transcribe. Every run now gets a fresh private folder instead.
    """
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    batch_id = f"batch-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    batch_input_dir = BATCH_ROOT / batch_id
    batch_input_dir.mkdir(parents=True, exist_ok=False)

    queue_data = [
        {
            "aweme_id": item.aweme_id,
            "title": item.title,
            "author": item.author,
            "url": item.url,
            "source": item.source,
        }
        for item in choices
    ]
    SELECTION_FILE.write_text(json.dumps(queue_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # Cookie data is intentionally not copied. The vendored downloader reads
    # the existing local config/cookies.json created by the login step.
    config = {
        "link": [item.url for item in choices],
        "path": str(batch_input_dir.resolve()),
        "mode": [],
        "thread": 2,
        "retry_times": 3,
        "cover": False,
        "music": False,
        "avatar": False,
        "json": True,
        # Temporary transcription never needs human-readable download names.
        # Keeping only the stable ID avoids Windows long-path failures for
        # long Douyin captions, and makes cleanup both faster and safer.
        "folderstyle": False,
        "group_by_mode": False,
        "filename_template": "{id}",
        "folder_template": "{id}",
        # Speech transcription does not benefit from an ultra-HD video.  A
        # smaller stream reduces waiting time and transient disk usage.
        "video_quality": "540p",
        # The downloader database is unnecessary for disposable batches and
        # should not be allowed to influence a future transcription run.
        "database": False,
        # Keep the downloader's bookkeeping separate from the transcription
        # history.  Its "success" means only "media downloaded", not
        # "transcribed", and must never make the desktop app skip a video.
        "processed_state_db": str((batch_input_dir / "downloader_state.sqlite3").resolve()),
        "auto_cookie": True,
        "transcript": {"enabled": False},
        "browser_fallback": {"enabled": True, "headless": False},
        "progress": {"quiet_logs": False},
    }
    DOWNLOAD_CONFIG.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # Resolve paths from the ordinary local configuration first, then store
    # absolute paths so the per-batch config can safely live under runtime/.
    # A fresh clone has no private config.yml yet. The public example has the
    # same safe defaults and lets the application create an isolated batch
    # before the first login/setup has written the local config file.
    from process_likes import load_config

    process_config_source = LOCAL_CONFIG if LOCAL_CONFIG.exists() else CONFIG_EXAMPLE
    process_config, _ = load_config(process_config_source)
    process_config["input_dir"] = str(batch_input_dir.resolve())
    for key in ("output_txt", "pending_txt", "state_db", "model_dir"):
        process_config[key] = str(process_config[key])
    PROCESS_CONFIG.write_text(
        yaml.safe_dump(process_config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return BatchJob(SELECTION_FILE, DOWNLOAD_CONFIG, PROCESS_CONFIG, batch_input_dir)


def cleanup_completed_batch(input_dir: Path) -> bool:
    """Delete only a fully successful temporary batch, never failed media."""
    if not input_dir.exists():
        return True
    leftovers = [
        path
        for path in input_dir.rglob("*")
        if path.is_file()
        and (path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".avi"} or path.name.endswith("_data.json"))
    ]
    if leftovers:
        return False
    shutil.rmtree(input_dir, ignore_errors=True)
    return not input_dir.exists()


def command_for_local_processing(ids_file: Path, process_config: Path = LOCAL_CONFIG) -> list[str]:
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    return [
        str(python),
        str(ROOT / "process_likes.py"),
        "--config",
        str(process_config),
        "--only-ids-file",
        str(ids_file),
    ]


def command_for_login() -> list[str]:
    """Run the existing local-only cookie capture helper without a terminal window."""
    return [
        "uv", "run", "--project", ".", "--extra", "browser", "python",
        "tools/cookie_fetcher.py", "--config", "config.local.yml",
    ]


class DouyinDesktopApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("\u6296\u97f3\u89c6\u9891\u8f6c\u6587\u5b57\u5de5\u5177")
        self.geometry("1240x790")
        self.minsize(1000, 660)
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        if sv_ttk is not None:
            sv_ttk.set_theme("light", self)
        self.style = ttk.Style(self)
        self._configure_visual_styles()
        self.choices: dict[str, VideoChoice] = {}
        self.checked: set[str] = set()
        self.active_job = False
        self._closing = False
        self._login_confirmed: threading.Event | None = None
        self._login_cancelled: threading.Event | None = None
        self._login_process: subprocess.Popen[str] | None = None
        self._command_process: subprocess.Popen[str] | None = None
        self._process_lock = threading.Lock()
        self.status_var = tk.StringVar(value="请选择抓取方式：喜欢、收藏，或粘贴单个视频链接。")
        self.progress_var = tk.DoubleVar(value=0)
        self._progress_current = 0
        self._progress_total = 0
        self.count_var = tk.StringVar(value="50")
        self.single_url_var = tk.StringVar()
        self.filter_var = tk.StringVar()
        self.filter_info_var = tk.StringVar(value="\u663e\u793a\u5168\u90e8 0 \u6761")
        self.login_state_var = tk.StringVar(value="\u6b63\u5728\u68c0\u67e5\u767b\u5f55\u72b6\u6001\u2026")
        self.filter_var.trace_add("write", lambda *_args: self._render_choices())
        self._build_ui()
        self._refresh_login_status()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_visual_styles(self) -> None:
        """Use a compact hierarchy so the primary action stays visible."""
        self.style.configure("App.TFrame", background="#F4F6FA")
        self.style.configure("Header.TFrame", background="#FFFFFF")
        self.style.configure("Toolbar.TFrame", background="#FFFFFF")
        self.style.configure("Action.TFrame", background="#EAF2FF")
        self.style.configure("Title.TLabel", background="#FFFFFF", foreground="#172B4D", font=("Microsoft YaHei UI", 20, "bold"))
        self.style.configure("Subtitle.TLabel", background="#FFFFFF", foreground="#667085", font=("Microsoft YaHei UI", 10))
        self.style.configure("LoginOk.TLabel", background="#FFFFFF", foreground="#17803D", font=("Microsoft YaHei UI", 10, "bold"))
        self.style.configure("LoginPending.TLabel", background="#FFFFFF", foreground="#B54708", font=("Microsoft YaHei UI", 10, "bold"))
        self.style.configure("SectionTitle.TLabel", foreground="#1D2939", font=("Microsoft YaHei UI", 11, "bold"))
        self.style.configure("Hint.TLabel", foreground="#667085", font=("Microsoft YaHei UI", 9))
        self.style.configure("Status.TLabel", foreground="#344054", font=("Microsoft YaHei UI", 10))
        self.style.configure("Metric.TLabel", background="#FFFFFF", foreground="#344054", font=("Microsoft YaHei UI", 10, "bold"))
        self.style.configure("MetricCaption.TLabel", background="#FFFFFF", foreground="#667085", font=("Microsoft YaHei UI", 9))
        self.style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(15, 7))
        self.style.configure("Treeview", rowheight=32, font=("Microsoft YaHei UI", 10), background="#FFFFFF", fieldbackground="#FFFFFF")
        self.style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        self.style.map("Treeview", background=[("selected", "#DCEBFF")], foreground=[("selected", "#173B67")])

    def _build_metric_card(self, parent: ttk.Frame, value_var: tk.StringVar, caption: str) -> None:
        card = ttk.Frame(parent, style="Toolbar.TFrame", padding=(12, 7))
        card.pack(side="left", padx=(0, 12))
        ttk.Label(card, textvariable=value_var, style="Metric.TLabel").pack(side="left")
        ttk.Label(card, text=caption, style="MetricCaption.TLabel").pack(side="left", padx=(5, 0))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, style="App.TFrame", padding=14)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="Header.TFrame", padding=(18, 13))
        header.pack(fill="x")
        header_actions = ttk.Frame(header, style="Header.TFrame")
        header_actions.pack(side="right", anchor="ne")
        self.login_state_label = ttk.Label(header_actions, textvariable=self.login_state_var, style="LoginPending.TLabel")
        self.login_state_label.pack(anchor="e")
        self.login_button = ttk.Button(header_actions, text="重新登录", command=self._start_login)
        self.login_button.pack(anchor="e", pady=(6, 0))
        ttk.Label(header, text="抖音视频转文字", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="先读取清单，再勾选；只处理已选视频，视频转写成功后会自动清理。", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))

        metrics = ttk.Frame(outer, style="Toolbar.TFrame", padding=(14, 5))
        metrics.pack(fill="x", pady=(8, 8))
        self.metric_total_var, self.metric_selected_var, self.metric_done_var = tk.StringVar(value="0"), tk.StringVar(value="0"), tk.StringVar(value="0")
        self._build_metric_card(metrics, self.metric_total_var, "已抓取")
        self._build_metric_card(metrics, self.metric_selected_var, "已勾选")
        self._build_metric_card(metrics, self.metric_done_var, "已转写")
        ttk.Label(metrics, text="数据、登录信息与转写结果仅保存在本机。", style="Hint.TLabel").pack(side="right")

        controls = ttk.LabelFrame(outer, text=" 第 1 步  读取视频清单 ", padding=12)
        controls.pack(fill="x")
        ttk.Label(controls, text="抓取数量").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(controls, from_=1, to=10000, textvariable=self.count_var, width=8).grid(row=0, column=1, sticky="w", padx=(8, 10))
        self.scan_like_button = ttk.Button(controls, text="抓取喜欢", style="Primary.TButton", command=lambda: self._start_scan("like"))
        self.scan_like_button.grid(row=0, column=2, padx=(0, 7))
        self.scan_collect_button = ttk.Button(controls, text="抓取收藏", command=lambda: self._start_scan("collect"))
        self.scan_collect_button.grid(row=0, column=3, padx=(0, 14))
        ttk.Label(controls, text="仅读取标题、作者、简介和链接，不会立即下载。", style="Hint.TLabel").grid(row=0, column=4, sticky="w")
        ttk.Separator(controls, orient="horizontal").grid(row=1, column=0, columnspan=5, sticky="ew", pady=9)
        ttk.Label(controls, text="单个视频分享内容").grid(row=2, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.single_url_var).grid(row=2, column=1, columnspan=3, sticky="ew", padx=(8, 8))
        self.add_single_button = ttk.Button(controls, text="读取并勾选", command=self._add_single)
        self.add_single_button.grid(row=2, column=4, sticky="w")
        controls.columnconfigure(4, weight=1)

        action_card = ttk.Frame(outer, style="Action.TFrame", padding=(14, 10))
        action_card.pack(fill="x", pady=(10, 8))
        self.process_button = ttk.Button(action_card, text="转为文字（已选 0 条）", style="Primary.TButton", command=self._process_checked)
        self.process_button.pack(side="left")
        self.selection_label = ttk.Label(action_card, text="勾选后点击这里，即开始本机转写。", style="Hint.TLabel")
        self.selection_label.pack(side="left", padx=12)
        self.progress_text_var = tk.StringVar(value="等待开始")
        self.progress_label = ttk.Label(action_card, textvariable=self.progress_text_var, style="Hint.TLabel", width=20, anchor="e")
        self.progress_label.pack(side="right", padx=(0, 8))
        self.progress_bar = ttk.Progressbar(action_card, variable=self.progress_var, maximum=100, mode="determinate", length=200)
        self.progress_bar.pack(side="right", padx=(8, 10))

        list_header = ttk.Frame(outer, style="App.TFrame")
        list_header.pack(fill="x", pady=(0, 6))
        ttk.Label(list_header, text="第 2 步  勾选要转写的视频", style="SectionTitle.TLabel").pack(side="left")
        ttk.Label(list_header, text="在列表中搜索（可选）", style="Hint.TLabel").pack(side="left", padx=(14, 6))
        ttk.Entry(list_header, textvariable=self.filter_var, width=24).pack(side="left")
        ttk.Button(list_header, text="清除", command=lambda: self.filter_var.set("")).pack(side="left", padx=(5, 10))
        ttk.Label(list_header, textvariable=self.filter_info_var, style="Hint.TLabel").pack(side="left")
        ttk.Button(list_header, text="全选", command=self._select_all).pack(side="right")
        ttk.Button(list_header, text="反选", command=self._invert_selection).pack(side="right", padx=5)
        ttk.Button(list_header, text="取消全选", command=self._clear_all).pack(side="right")
        ttk.Button(list_header, text="查看详情", command=self._show_details).pack(side="right", padx=(0, 14))
        ttk.Button(list_header, text="清空列表", command=self._clear_choices).pack(side="right", padx=(0, 5))

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True)
        columns = ("checked", "title", "author", "source", "caption", "url")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended", height=6)
        headings = {"checked": "勾选", "title": "标题", "author": "作者", "source": "来源", "caption": "原始文案 / 简介", "url": "视频链接"}
        widths = {"checked": 58, "title": 235, "author": 116, "source": 132, "caption": 340, "url": 230}
        for name in columns:
            self.table.heading(name, text=headings[name])
            self.table.column(name, width=widths[name], anchor="w", stretch=name in {"title", "caption", "url"})
        self.table.tag_configure("odd", background="#F8FAFC")
        self.table.tag_configure("even", background="#FFFFFF")
        ybar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        xbar = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.table.bind("<ButtonRelease-1>", self._on_table_click)
        self.table.bind("<Double-1>", lambda _event: self._show_details())

        bottom = ttk.Frame(outer, style="App.TFrame")
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Label(bottom, textvariable=self.status_var, style="Status.TLabel").pack(side="left", fill="x", expand=True)
        ttk.Button(bottom, text="打开输出文件夹", command=self._open_output_folder).pack(side="right", padx=(8, 0))
        self.open_pending_button = ttk.Button(bottom, text="打开待 Codex 总结文本", command=self._open_pending)
        self.open_pending_button.pack(side="right")
        self._refresh_metrics()

    def _refresh_login_status(self) -> None:
        if has_saved_douyin_login():
            self.login_state_var.set("● 已登录")
            self.login_state_label.configure(style="LoginOk.TLabel")
        else:
            self.login_state_var.set("○ 未登录")
            self.login_state_label.configure(style="LoginPending.TLabel")

    def _refresh_metrics(self) -> None:
        self.metric_total_var.set(str(len(self.choices)))
        self.metric_selected_var.set(str(len(self.checked)))
        self.metric_done_var.set(str(len(completed_video_ids())))

    def _requested_count(self) -> int | None:
        try:
            number = int(self.count_var.get())
        except ValueError:
            messagebox.showwarning("数量不正确", "请输入 1 到 10000 之间的整数。", parent=self)
            return None
        if not 1 <= number <= 10000:
            messagebox.showwarning("数量不正确", "请输入 1 到 10000 之间的整数。", parent=self)
            return None
        return number

    def _on_table_click(self, event: tk.Event[Any]) -> None:
        if self.table.identify_column(event.x) != "#1":
            return
        item_id = self.table.identify_row(event.y)
        if item_id:
            self._toggle_ids([item_id])

    def _visible_choices(self) -> list[VideoChoice]:
        return filter_video_choices(list(self.choices.values()), self.filter_var.get())

    def _render_choices(self) -> None:
        selected_before = set(self.table.selection())
        visible_choices = self._visible_choices()
        self.table.delete(*self.table.get_children())
        for choice in visible_choices:
            aweme_id = choice.aweme_id
            marker = "\u2611" if aweme_id in self.checked else "\u2610"
            self.table.insert(
                "", "end", iid=aweme_id,
                values=(marker, choice.title, choice.author, choice.source, choice.caption, choice.url),
                tags=("odd" if len(self.table.get_children()) % 2 else "even",),
            )
        retained = [item_id for item_id in selected_before if item_id in self.table.get_children()]
        if retained:
            self.table.selection_set(retained)
        if self.filter_var.get().strip():
            self.filter_info_var.set(f"\u7b5b\u9009\u540e\u663e\u793a {len(visible_choices)} / {len(self.choices)} \u6761")
        else:
            self.filter_info_var.set(f"\u663e\u793a\u5168\u90e8 {len(self.choices)} \u6761")
        self._refresh_selection_label()

    def _refresh_selection_label(self) -> None:
        selected_count = len(self.checked)
        self.process_button.configure(text=f"转为文字（已选 {selected_count} 条）")
        if selected_count:
            self.selection_label.configure(text="点击左侧按钮，开始本机下载与转写。")
        else:
            self.selection_label.configure(text="先在表格里勾选视频，再点击左侧按钮。")
        self._refresh_metrics()

    def _toggle_ids(self, item_ids: list[str]) -> None:
        for item_id in item_ids:
            if item_id in self.checked:
                self.checked.remove(item_id)
            else:
                self.checked.add(item_id)
        self._render_choices()

    def _select_all(self) -> None:
        self.checked.update(choice.aweme_id for choice in self._visible_choices())
        self._render_choices()

    def _clear_all(self) -> None:
        self.checked.clear()
        self._render_choices()

    def _invert_selection(self) -> None:
        visible_ids = {choice.aweme_id for choice in self._visible_choices()}
        self.checked.symmetric_difference_update(visible_ids)
        self._render_choices()

    def _clear_choices(self) -> None:
        if self.active_job:
            return
        self.choices.clear()
        self.checked.clear()
        self.filter_var.set("")
        self._render_choices()
        self.status_var.set("\u5217\u8868\u5df2\u6e05\u7a7a\u3002\u53ef\u4ee5\u91cd\u65b0\u6293\u53d6\u559c\u6b22\u3001\u6536\u85cf\uff0c\u6216\u6dfb\u52a0\u5355\u4e2a\u89c6\u9891\u94fe\u63a5\u3002")

    def _start_scan(self, kind: str) -> None:
        count = self._requested_count()
        if count is None or self.active_job:
            return
        self._set_busy(True, "正在连接抖音并读取列表…")
        excluded = completed_video_ids()

        def worker(report: Callable[[str], None]) -> list[VideoChoice]:
            if kind == "like":
                return asyncio.run(fetch_liked_videos(count, excluded, report))
            return asyncio.run(fetch_collected_videos(count, excluded, report))

        def done(result: list[VideoChoice]) -> None:
            self.choices = {item.aweme_id: item for item in result}
            self.checked.clear()
            self._render_choices()
            self._set_busy(False, f"已列出 {len(result)} 条可选择视频。点击每行最左侧空格即可勾选。")
            if not result:
                messagebox.showinfo("没有可选视频", "没有读取到可转写的新视频。可能列表为空、已全部处理，或登录状态已过期。", parent=self)

        self._run_background(worker, done)

    def _add_single(self) -> None:
        if self.active_job:
            return
        url = self.single_url_var.get().strip()
        if not url:
            messagebox.showwarning("缺少链接", "请先粘贴一个抖音视频分享链接。", parent=self)
            return
        self._set_busy(True, "正在读取这个视频的信息…")
        excluded = completed_video_ids()

        def worker(_report: Callable[[str], None]) -> VideoChoice:
            return asyncio.run(fetch_single_video(url, excluded))

        def done(result: VideoChoice) -> None:
            self.choices[result.aweme_id] = result
            self.checked.add(result.aweme_id)
            self.single_url_var.set("")
            self._render_choices()
            self._set_busy(False, "已添加单个视频，并自动勾选。")

        self._run_background(worker, done)

    def _show_details(self) -> None:
        selected = list(self.table.selection())
        if not selected:
            messagebox.showinfo("请选择一条视频", "先在表格里选中一条视频，再查看原始文案和链接。", parent=self)
            return
        choice = self.choices.get(selected[0])
        if not choice:
            return
        dialog = tk.Toplevel(self)
        dialog.title("视频信息")
        dialog.geometry("760x440")
        dialog.transient(self)
        ttk.Label(dialog, text=choice.title, font=("Microsoft YaHei UI", 13, "bold"), wraplength=700).pack(anchor="w", padx=14, pady=(14, 8))
        text = tk.Text(dialog, wrap="word", height=17)
        text.pack(fill="both", expand=True, padx=14, pady=4)
        text.insert("1.0", f"作者：{choice.author}\n来源：{choice.source}\n链接：{choice.url}\n\n原始文案 / 简介：\n{choice.caption}")
        text.configure(state="disabled")
        ttk.Button(dialog, text="关闭", command=dialog.destroy).pack(pady=(4, 14))

    def _process_checked(self) -> None:
        if self.active_job:
            return
        selected = [self.choices[aweme_id] for aweme_id in self.checked if aweme_id in self.choices]
        if not selected:
            messagebox.showinfo("还没有选择", "请先勾选至少一个视频。", parent=self)
            return
        if not messagebox.askyesno(
            "确认开始", f"将临时下载并在本机转写所选的 {len(selected)} 条视频。\n\n转写成功后，原视频和元数据会自动删除。是否继续？", parent=self
        ):
            return
        self._set_busy(True, f"准备处理 {len(selected)} 条已选视频…")
        self._start_progress_indicator()

        def worker(report: Callable[[str], None]) -> tuple[int, str]:
            batch = write_selection_files(selected)
            report(f"{PROGRESS_PHASE_PREFIX}download")
            report("正在临时下载已选视频…")
            download_command = ["uv", "run", "--project", ".", "python", "run.py", "--config", str(batch.download_config_path)]
            code, output = run_command(download_command, VENDOR_ROOT, report, self._remember_command_process)
            if code != 0:
                raise RuntimeError("下载没有完成。请检查网络或重新登录抖音。\n\n" + tail(output))
            repaired = repair_missing_batch_metadata(selected, batch.input_dir)
            if repaired:
                report(f"已为 {repaired} 条已下载视频补齐本地元数据，正在转写…")
            ready_ids = downloaded_selection_ids(selected, batch.input_dir)
            if not ready_ids:
                raise RuntimeError(
                    "抖音没有成功下载任何已选视频，因此不会生成空白 TXT。"
                    "\n\n请稍后重试；如果连续失败，请先用“重新登录抖音”完成登录。"
                    "\n\n下载程序最后提示：\n" + tail(output)
                )
            report(f"已获得 {len(ready_ids)} 条可以转写的视频，正在本机转写…")
            report(f"{PROGRESS_PHASE_PREFIX}transcribe:{len(ready_ids)}")
            code, output = run_command(
                command_for_local_processing(batch.queue_path, batch.process_config_path),
                ROOT,
                report,
                self._remember_command_process,
            )
            if code != 0:
                raise RuntimeError("本机转写没有完成。临时视频会保留，可稍后重试。\n\n" + tail(output))
            transcribed_count = pending_report_video_count()
            if transcribed_count == 0:
                raise RuntimeError("视频已下载，但没有任何视频转写成功，因此未生成可用的 TXT。\n\n" + tail(output))
            cleanup_completed_batch(batch.input_dir)
            return transcribed_count, output

        def done(result: tuple[int, str]) -> None:
            count, _ = result
            self._finish_progress_indicator(success=True)
            self._set_busy(False, f"已完成 {count} 条视频的本机转写，文字已生成。")
            if PENDING_TXT.exists():
                self._open_pending()
                messagebox.showinfo(
                    "转写完成",
                    "待 Codex 总结文本已打开。\n\n请回到当前 Codex 对话，发送：\n请总结待 Codex 总结文件\n\n原视频已在成功转写后自动删除。",
                    parent=self,
                )
            else:
                messagebox.showwarning("未找到文本", "转写流程已结束，但没有找到待 Codex 总结文本。请查看状态提示。", parent=self)

        self._run_background(worker, done)

    def _start_login(self) -> None:
        """Let the user refresh a local Douyin login without exposing a terminal."""
        if self.active_job:
            return
        self._login_confirmed = threading.Event()
        self._login_cancelled = threading.Event()
        self._set_busy(True, "\u6b63\u5728\u51c6\u5907\u672c\u5730\u6296\u97f3\u767b\u5f55\u7a97\u53e3\u2026")
        self._start_progress_indicator("login")

        dialog = tk.Toplevel(self)
        dialog.title("\u91cd\u65b0\u767b\u5f55\u6296\u97f3")
        dialog.geometry("560x300")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        ttk.Label(
            dialog, text="\u8bf7\u5728\u968f\u540e\u6253\u5f00\u7684\u6296\u97f3\u7a97\u53e3\u4e2d\u5b8c\u6210\u767b\u5f55",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(anchor="w", padx=24, pady=(24, 8))
        ttk.Label(
            dialog,
            text="\u767b\u5f55\u6210\u529f\u5e76\u56de\u5230\u6296\u97f3\u9996\u9875\u540e\uff0c\u518d\u56de\u5230\u8fd9\u91cc\u70b9\u51fb\u201c\u6211\u5df2\u767b\u5f55\uff0c\u4fdd\u5b58\u201d\u3002\n\u767b\u5f55\u4fe1\u606f\u53ea\u4fdd\u5b58\u5230\u672c\u673a\uff0c\u4e0d\u4f1a\u4e0a\u4f20\u89c6\u9891\u3001\u97f3\u9891\u6216\u8d26\u53f7\u4fe1\u606f\u3002",
            justify="left",
            wraplength=500,
        ).pack(anchor="w", padx=24)
        button_row = ttk.Frame(dialog)
        button_row.pack(fill="x", padx=24, pady=(24, 0))
        confirm = ttk.Button(button_row, text="\u6211\u5df2\u767b\u5f55\uff0c\u4fdd\u5b58", style="Accent.TButton")
        confirm.pack(side="right")

        def cancel() -> None:
            assert self._login_cancelled is not None
            self._login_cancelled.set()
            self._stop_login_process()
            if dialog.winfo_exists():
                dialog.destroy()

        ttk.Button(button_row, text="\u53d6\u6d88", command=cancel).pack(side="right", padx=(0, 8))

        def confirm_login() -> None:
            assert self._login_confirmed is not None
            self._login_confirmed.set()
            confirm.configure(state="disabled")
            self.status_var.set("\u6b63\u5728\u4fdd\u5b58\u8fd9\u6b21\u672c\u5730\u767b\u5f55\u4fe1\u606f\u2026")

        confirm.configure(command=confirm_login)
        dialog.protocol("WM_DELETE_WINDOW", cancel)

        def worker(report: Callable[[str], None]) -> bool:
            process = subprocess.Popen(
                command_for_login(), cwd=str(VENDOR_ROOT), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._remember_login_process(process)
            try:
                if self._closing or (self._login_cancelled is not None and self._login_cancelled.is_set()):
                    terminate_process_tree(process)
                    raise RuntimeError("\u5df2\u53d6\u6d88\u91cd\u65b0\u767b\u5f55\u3002")
                report("\u7b49\u5f85\u4f60\u5728\u6296\u97f3\u7a97\u53e3\u5b8c\u6210\u767b\u5f55\u2026")
                while True:
                    if self._login_cancelled is not None and self._login_cancelled.is_set():
                        raise RuntimeError("\u5df2\u53d6\u6d88\u91cd\u65b0\u767b\u5f55\u3002")
                    if self._login_confirmed is not None and self._login_confirmed.is_set():
                        break
                    if process.poll() is not None:
                        output = process.stdout.read() if process.stdout is not None else ""
                        raise RuntimeError("\u767b\u5f55\u7a97\u53e3\u6ca1\u6709\u4fdd\u6301\u6253\u5f00\u3002\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002\n\n" + tail(output))
                    time.sleep(0.2)
                if process.stdin is not None:
                    process.stdin.write("\n")
                    process.stdin.flush()
                    process.stdin.close()
                output = process.stdout.read() if process.stdout is not None else ""
                code = process.wait()
                if code != 0:
                    raise RuntimeError("\u4fdd\u5b58\u672c\u5730\u767b\u5f55\u4fe1\u606f\u5931\u8d25\u3002\u8bf7\u91cd\u65b0\u767b\u5f55\u540e\u518d\u8bd5\u3002\n\n" + tail(output))

                async def validate_login() -> None:
                    client, _, _, _ = await _open_api_client()
                    await client.__aexit__(None, None, None)

                asyncio.run(validate_login())
                return True
            finally:
                self._clear_login_process(process)

        def done(_result: bool) -> None:
            if dialog.winfo_exists():
                dialog.destroy()
            self._finish_progress_indicator(success=True)
            self._refresh_login_status()
            self._set_busy(False, "\u6296\u97f3\u672c\u5730\u767b\u5f55\u5df2\u66f4\u65b0\uff0c\u73b0\u5728\u53ef\u4ee5\u6293\u53d6\u559c\u6b22\u6216\u6536\u85cf\u89c6\u9891\u3002")
            messagebox.showinfo(
                "\u767b\u5f55\u5df2\u66f4\u65b0",
                "\u672c\u5730\u767b\u5f55\u4fe1\u606f\u5df2\u4fdd\u5b58\u3002\u73b0\u5728\u53ef\u4ee5\u6293\u53d6\u559c\u6b22\u3001\u6536\u85cf\uff0c\u6216\u6dfb\u52a0\u5355\u4e2a\u89c6\u9891\u3002",
                parent=self,
            )

        self._run_background(worker, done)

    def _open_pending(self) -> None:
        if not PENDING_TXT.exists():
            messagebox.showinfo("暂时没有文本", "尚未生成待 Codex 总结文本。请先选择视频并转为文字。", parent=self)
            return
        os.startfile(PENDING_TXT)  # type: ignore[attr-defined]

    def _open_output_folder(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(OUTPUT_DIR)  # type: ignore[attr-defined]

    def _start_progress_indicator(self, phase: str = "download") -> None:
        self._progress_current = 0
        self._progress_total = 0
        self._progress_phase = phase
        self.progress_var.set(0)
        self.progress_text_var.set("\u7b49\u5f85\u5b8c\u6210\u767b\u5f55\u2026" if phase == "login" else "\u6b63\u5728\u4e0b\u8f7d\u2026")
        self.progress_bar.configure(mode="indeterminate", maximum=100)
        self.progress_bar.start(12)

    def _finish_progress_indicator(self, success: bool) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", maximum=100)
        self.progress_var.set(100 if success else 0)
        if success:
            if self._progress_total:
                self.progress_text_var.set(f"\u5b8c\u6210 {self._progress_total}/{self._progress_total}")
            else:
                self.progress_text_var.set("\u4e0b\u8f7d\u5e76\u8f6c\u5199\u5b8c\u6210")
        else:
            self.progress_text_var.set("\u672c\u6b21\u672a\u5b8c\u6210")

    def _handle_progress_message(self, message: str) -> None:
        if message.startswith(PROGRESS_PHASE_PREFIX):
            phase = message[len(PROGRESS_PHASE_PREFIX):]
            if phase in {"download", "login"}:
                self._progress_phase = phase
                self.progress_var.set(0)
                self.progress_text_var.set("\u7b49\u5f85\u5b8c\u6210\u767b\u5f55\u2026" if phase == "login" else "\u6b63\u5728\u4e0b\u8f7d\u2026")
                self.progress_bar.configure(mode="indeterminate", maximum=100)
                self.progress_bar.start(12)
            elif phase.startswith("transcribe:"):
                self._progress_phase = "transcribe"
                try:
                    self._progress_total = max(int(phase.split(":", 1)[1]), 1)
                except ValueError:
                    self._progress_total = 1
                self._progress_current = 0
                self.progress_bar.stop()
                self.progress_bar.configure(mode="determinate", maximum=self._progress_total)
                self.progress_var.set(0)
                self.progress_text_var.set(f"\u6b63\u5728\u8f6c\u5199 0/{self._progress_total}")
            return

        clean_message = ANSI_ESCAPE_RE.sub("", message).strip()
        if self._progress_phase == "transcribe":
            progress = parse_transcription_progress(clean_message)
            if progress:
                current, total, detail = progress
                self._progress_current = current
                self._progress_total = total
                self.progress_bar.stop()
                self.progress_bar.configure(mode="determinate", maximum=total)
                self.progress_var.set(max(0, min(current, total)))
                self.progress_text_var.set(f"\u6b63\u5728\u8f6c\u5199 {current}/{total}")
                self.status_var.set(f"\u6b63\u5728\u8f6c\u5199\u7b2c {current}/{total} \u6761\uff1a{detail[:70]}")
                return
        self.status_var.set(clean_message)

    def _remember_command_process(self, process: subprocess.Popen[str] | None) -> None:
        with self._process_lock:
            self._command_process = process
            should_stop = self._closing and process is not None
        if should_stop:
            terminate_process_tree(process)

    def _remember_login_process(self, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            self._login_process = process
            should_stop = self._closing or (self._login_cancelled is not None and self._login_cancelled.is_set())
        if should_stop:
            terminate_process_tree(process)

    def _clear_login_process(self, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            if self._login_process is process:
                self._login_process = None

    def _stop_login_process(self) -> None:
        with self._process_lock:
            process = self._login_process
        terminate_process_tree(process)

    def _stop_current_work(self) -> None:
        """Cancel login, download, and transcription before the app exits."""
        self._closing = True
        if self._login_cancelled is not None:
            self._login_cancelled.set()
        with self._process_lock:
            command_process = self._command_process
            login_process = self._login_process
        terminate_process_tree(command_process)
        terminate_process_tree(login_process)

    def _set_busy(self, busy: bool, status: str) -> None:
        self.active_job = busy
        self.status_var.set(status)
        state = "disabled" if busy else "normal"
        for control in (self.process_button, self.scan_like_button, self.scan_collect_button, self.add_single_button, self.login_button):
            control.configure(state=state)

    def _run_background(self, job: Callable[[Callable[[str], None]], Any], done: Callable[[Any], None]) -> None:
        messages: queue.Queue[tuple[str, Any]] = queue.Queue()

        def report(message: str) -> None:
            messages.put(("progress", message))

        def runner() -> None:
            try:
                messages.put(("done", job(report)))
            except Exception as exc:  # display the useful final error in the UI
                messages.put(("error", exc))

        threading.Thread(target=runner, daemon=True).start()

        def poll() -> None:
            if self._closing:
                return
            try:
                while True:
                    if self._closing:
                        return
                    kind, payload = messages.get_nowait()
                    if kind == "progress":
                        self._handle_progress_message(str(payload))
                    elif kind == "done":
                        done(payload)
                        return
                    else:
                        self._finish_progress_indicator(success=False)
                        self._set_busy(False, "本次操作没有完成。")
                        messagebox.showerror("没有完成", str(payload), parent=self)
                        return
            except queue.Empty:
                self.after(120, poll)

        self.after(120, poll)

    def _on_close(self) -> None:
        # The close button behaves like a normal desktop app: cancel and exit.
        self._stop_current_work()
        self.destroy()


def run_command(
    command: list[str],
    cwd: Path,
    report: Callable[[str], None],
    on_process_started: Callable[[subprocess.Popen[str] | None], None] | None = None,
) -> tuple[int, str]:
    """Run a local helper without showing a console, while forwarding status."""
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if on_process_started is not None:
        on_process_started(process)
    output: list[str] = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if line:
                output.append(line)
                report(line)
        return process.wait(), "\n".join(output)
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if on_process_started is not None:
            on_process_started(None)


def terminate_process_tree(process: subprocess.Popen[str] | None) -> None:
    """Stop a local helper and every child process it started on Windows."""
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                return
        except OSError:
            pass
    try:
        process.terminate()
    except OSError:
        pass


def tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.splitlines()[-lines:])


def main() -> int:
    app = DouyinDesktopApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
