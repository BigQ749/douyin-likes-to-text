"""Prepare temporary Douyin likes for a manual Codex summary step.

Videos are transcribed on this computer. The source media is deleted after a
successful transcription. The text remains locally until the user asks Codex in
this conversation to create the final summaries.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("缺少 PyYAML。请运行：uv sync") from exc

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
DEFAULT_CONFIG = {
    "input_dir": "input",
    "output_txt": "output/抖音点赞视频总结.txt",
    "pending_txt": "output/待Codex总结.txt",
    "state_db": "output/processing_state.sqlite3",
    "model_dir": "models",
    "transcription": {
        "model": "small",
        "device": "cpu",
        "compute_type": "int8",
        "language": "zh",
    },
    "processing": {
        "delete_video_after_transcription": True,
        "delete_metadata_after_transcription": True,
        "max_videos": 0,
    },
}


@dataclass(frozen=True)
class VideoItem:
    video_path: Path
    metadata_path: Path
    aweme_id: str
    title: str
    author: str
    url: str


def merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(config_path: Path) -> tuple[dict[str, Any], Path]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是 YAML 对象。")
    config = merge_dict(DEFAULT_CONFIG, raw)
    base = config_path.parent.resolve()
    for key in ("input_dir", "output_txt", "pending_txt", "state_db", "model_dir"):
        candidate = Path(str(config[key]))
        config[key] = (base / candidate).resolve() if not candidate.is_absolute() else candidate
    return config, base


def create_database(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_videos (
            aweme_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            author TEXT,
            url TEXT NOT NULL,
            summary TEXT,
            status TEXT NOT NULL,
            error TEXT,
            processed_at TEXT NOT NULL
        )
        """
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(processed_videos)")}
    if "transcript" not in columns:
        connection.execute("ALTER TABLE processed_videos ADD COLUMN transcript TEXT")
    connection.commit()
    return connection


def parse_metadata(metadata_path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def first_video_for(metadata_path: Path) -> Path | None:
    stem = metadata_path.name[: -len("_data.json")]
    candidates = [
        path
        for path in metadata_path.parent.glob(f"{stem}.*")
        if path.suffix.lower() in VIDEO_SUFFIXES and path.is_file()
    ]
    return sorted(candidates)[0] if candidates else None


def find_items(
    input_dir: Path, unavailable: set[str], allowed_ids: set[str] | None = None
) -> list[VideoItem]:
    items: list[VideoItem] = []
    if not input_dir.exists():
        return items
    for metadata_path in sorted(input_dir.rglob("*_data.json")):
        data = parse_metadata(metadata_path)
        aweme_id = str(data.get("aweme_id") or data.get("aweme_id_str") or "").strip()
        if not aweme_id or aweme_id in unavailable:
            continue
        if allowed_ids is not None and aweme_id not in allowed_ids:
            continue
        video_path = first_video_for(metadata_path)
        if video_path is None:
            continue
        author_data = data.get("author") if isinstance(data.get("author"), dict) else {}
        title = str(data.get("desc") or "").strip() or video_path.stem
        author = str(author_data.get("nickname") or "").strip()
        items.append(VideoItem(video_path, metadata_path, aweme_id, title, author, f"https://www.douyin.com/video/{aweme_id}"))
    return items


def transcribe(model: Any, video_path: Path, language: str) -> str:
    segments, _ = model.transcribe(
        str(video_path), language=language or None, vad_filter=True, beam_size=5, condition_on_previous_text=False
    )
    transcript = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
    return transcript or "[\u672a\u68c0\u6d4b\u5230\u53ef\u8bc6\u522b\u8bed\u97f3\uff1a\u89c6\u9891\u53ef\u80fd\u6ca1\u6709\u4eba\u58f0\u3001\u97f3\u91cf\u592a\u4f4e\u6216\u80cc\u666f\u97f3\u592a\u5f3a\u3002]"


def delete_if_requested(path: Path, enabled: bool) -> None:
    if enabled and path.exists():
        path.unlink()


def prune_empty_parents(path: Path, stop_at: Path) -> None:
    current = path.parent
    while current != stop_at and stop_at in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def save_transcript_pending(connection: sqlite3.Connection, item: VideoItem, transcript: str) -> None:
    connection.execute(
        """
        INSERT INTO processed_videos (aweme_id, title, author, url, summary, status, error, processed_at, transcript)
        VALUES (?, ?, ?, ?, NULL, 'transcribed', NULL, ?, ?)
        ON CONFLICT(aweme_id) DO UPDATE SET
            title=excluded.title, author=excluded.author, url=excluded.url,
            summary=NULL, transcript=excluded.transcript, status='transcribed', error=NULL,
            processed_at=excluded.processed_at
        """,
        (item.aweme_id, item.title, item.author, item.url, datetime.now().isoformat(timespec="seconds"), transcript),
    )
    connection.commit()


def save_failure(connection: sqlite3.Connection, item: VideoItem, error: Exception) -> None:
    connection.execute(
        """
        INSERT INTO processed_videos (aweme_id, title, author, url, summary, status, error, processed_at, transcript)
        VALUES (?, ?, ?, ?, NULL, 'failed', ?, ?, NULL)
        ON CONFLICT(aweme_id) DO UPDATE SET
            status='failed', error=excluded.error, processed_at=excluded.processed_at
        """,
        (item.aweme_id, item.title, item.author, item.url, str(error), datetime.now().isoformat(timespec="seconds")),
    )
    connection.commit()


def write_report(connection: sqlite3.Connection, output_path: Path) -> int:
    rows = connection.execute(
        """
        SELECT title, author, url, summary
        FROM processed_videos
        WHERE status='success'
        ORDER BY processed_at, aweme_id
        """
    ).fetchall()
    lines = [
        "抖音点赞视频总结",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "说明：此文件只包含已由 Codex 完成整理的条目。",
        "",
    ]
    for number, (title, author, url, summary) in enumerate(rows, start=1):
        lines.extend([f"{number}. 标题：{title}", f"作者：{author or '未知'}", f"链接：{url}", summary or "内容概述：无", ""])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8-sig")
    return len(rows)


def load_allowed_ids(ids_path: Path | None) -> set[str] | None:
    """Load the desktop app selection queue when one was provided."""
    if ids_path is None:
        return None
    try:
        raw = json.loads(ids_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read selected-video queue: {ids_path}") from exc
    if not isinstance(raw, list):
        raise ValueError("Selected-video queue must be a JSON list.")
    return {
        str(item.get("aweme_id") or "").strip()
        for item in raw
        if isinstance(item, dict) and str(item.get("aweme_id") or "").strip()
    }


def write_pending_report(
    connection: sqlite3.Connection, pending_path: Path, allowed_ids: set[str] | None = None
) -> int:
    """Write only the selected batch when ``allowed_ids`` is provided."""
    query = """
        SELECT aweme_id, title, author, url, transcript
        FROM processed_videos
        WHERE status='transcribed'
          AND transcript IS NOT NULL
          AND TRIM(transcript) <> ''
    """
    parameters: list[str] = []
    if allowed_ids is not None:
        if allowed_ids:
            placeholders = ", ".join("?" for _ in allowed_ids)
            query += f" AND aweme_id IN ({placeholders})"
            parameters = sorted(allowed_ids)
        else:
            query += " AND 1=0"
    query += " ORDER BY processed_at, aweme_id"
    rows = connection.execute(query, parameters).fetchall()
    lines = [
        "\u6296\u97f3\u89c6\u9891\u5f85 Codex \u603b\u7ed3",
        f"\u751f\u6210\u65f6\u95f4\uff1a{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "\u8bf4\u660e\uff1a\u8fd9\u4e00\u4efd\u6587\u4ef6\u53ea\u5305\u542b\u521a\u624d\u6210\u529f\u8f6c\u5199\u7684\u89c6\u9891\u3002\u89c6\u9891\u4ec5\u5728\u672c\u673a\u4e34\u65f6\u4e0b\u8f7d\u5e76\u8f6c\u5199\uff0c\u6210\u529f\u540e\u5df2\u5220\u9664\u3002",
        "\u8bf7\u5728 Codex \u5f53\u524d\u5bf9\u8bdd\u4e2d\u53d1\u9001\uff1a\u8bf7\u603b\u7ed3\u5f85 Codex \u603b\u7ed3\u6587\u4ef6\u3002",
        "",
    ]
    for number, (_, title, author, url, transcript) in enumerate(rows, start=1):
        lines.extend([
            f"===== \u89c6\u9891 {number} =====",
            f"\u6807\u9898\uff1a{title}",
            "\u4f5c\u8005\uff1a" + (author or "\u672a\u77e5"),
            f"\u94fe\u63a5\uff1a{url}",
            "\u8f6c\u5199\u5168\u6587\uff1a",
            (transcript or "[\u672a\u83b7\u53d6\u5230\u8f6c\u5199\u6587\u672c]").strip(),
            "",
        ])
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text("\n".join(lines), encoding="utf-8-sig")
    return len(rows)

def main() -> int:
    parser = argparse.ArgumentParser(description="本机转写抖音视频并生成待 Codex 总结文本；成功转写后删除临时视频。")
    parser.add_argument("--config", default="config.yml", help="YAML 配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只列出待处理视频，不下载模型、不转写、不删除")
    parser.add_argument("--only-ids-file", type=Path, help="Only transcribe IDs selected by the desktop app")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"未找到配置文件：{config_path}", file=sys.stderr)
        return 2
    config, _ = load_config(config_path)
    connection = create_database(config["state_db"])
    unavailable = {
        row[0]
        for row in connection.execute(
            """
            SELECT aweme_id
            FROM processed_videos
            WHERE status = 'transcribed'
              AND transcript IS NOT NULL
              AND TRIM(transcript) <> ''
            """
        )
    }
    allowed_ids = load_allowed_ids(args.only_ids_file)
    items = find_items(config["input_dir"], unavailable, allowed_ids)
    max_videos = int(config["processing"].get("max_videos", 0) or 0)
    if max_videos > 0:
        items = items[:max_videos]

    if not items:
        # The desktop queue can contain videos that were already handled in an
        # earlier batch. Do not recreate a new TXT from those old transcripts.
        pending_total = write_pending_report(
            connection, config["pending_txt"], set() if allowed_ids is not None else None
        )
        print(f"没有找到待转写视频。当前有 {pending_total} 条待 Codex 总结文字：{config['pending_txt']}")
        return 0

    print(f"找到 {len(items)} 条待处理视频。将逐条在本机转写、保存待 Codex 总结文字，并删除临时视频。")
    model: Any | None = None
    transcribed_ids: set[str] = set()
    for index, item in enumerate(items, start=1):
        print(f"{index}/{len(items)}  {item.title[:70]}")
        if args.dry_run:
            print(f"  视频：{item.video_path}\n  链接：{item.url}")
            continue
        try:
            if model is None:
                from faster_whisper import WhisperModel
                transcription_cfg = config["transcription"]
                print(f"  正在加载本地语音模型：{transcription_cfg['model']}")
                model = WhisperModel(
                    transcription_cfg["model"], device=transcription_cfg["device"],
                    compute_type=transcription_cfg["compute_type"], download_root=str(config["model_dir"])
                )
            transcript = transcribe(model, item.video_path, config["transcription"].get("language", "zh"))
            save_transcript_pending(connection, item, transcript)
            transcribed_ids.add(item.aweme_id)
            delete_if_requested(item.video_path, bool(config["processing"].get("delete_video_after_transcription", True)))
            delete_if_requested(item.metadata_path, bool(config["processing"].get("delete_metadata_after_transcription", True)))
            prune_empty_parents(item.video_path, config["input_dir"])
            print("  已保存待 Codex 总结文字，并清理临时视频。")
        except Exception as exc:
            save_failure(connection, item, exc)
            print(f"  转写失败，已保留文件以便重试：{exc}", file=sys.stderr)

    report_ids = transcribed_ids if allowed_ids is not None else None
    pending_total = write_pending_report(connection, config["pending_txt"], report_ids)
    print(f"完成。本次待 Codex 总结文字已写入：{config['pending_txt']}（当前共 {pending_total} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
