#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hmac
import io
import json
import mimetypes
import os
import random
import secrets
import sqlite3
import smtplib
import threading
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from itertools import count
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
RESPONSES_DIR = BASE_DIR / "responses"
CONFIG_PATH = DATA_DIR / "app_config.json"
CSV_RESULTS_PATH = RESPONSES_DIR / "survey_results.csv"
VIDEO_LINKS_CSV_PATH = DATA_DIR / "video_links.csv"
DATABASE_SOURCE_LABEL = "データベースランダム「Database Random」"

_database_lock = threading.Lock()
_csv_lock = threading.Lock()
_session_lock = threading.Lock()
_access_sessions: dict[str, dict[str, Any]] = {}


class AppError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.message = message
        self.status = int(status)


def bilingual(japanese: str, english: str) -> str:
    ja = str(japanese).strip()
    en = str(english).strip()
    if not ja:
        return en
    if not en:
        return ja
    return f"{ja}「{en}」"


def load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize_video_entry(entry: dict[str, Any], *, default_source_label: str) -> dict[str, str]:
    required_keys = ["id", "title", "url"]
    missing = [key for key in required_keys if key not in entry]
    if missing:
        raise AppError(
            bilingual(
                f"動画エントリに不足があります: {', '.join(missing)}",
                f"Video entry is missing required fields: {', '.join(missing)}",
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return {
        "id": str(entry["id"]),
        "title": str(entry["title"]),
        "url": str(entry["url"]),
        "description": str(entry.get("description", "")),
        "sourceLabel": str(entry.get("sourceLabel", default_source_label)),
        "videoGroup": str(entry.get("videoGroup", "")).strip(),
        "videoCode": str(entry.get("videoCode", entry["id"])).strip() or str(entry["id"]),
        "methodName": str(entry.get("methodName", "")).strip(),
        "sampleName": str(entry.get("sampleName", "")).strip(),
        "promptText": str(entry.get("promptText", "")).strip(),
    }


def load_config() -> dict[str, Any]:
    config = load_json_file(CONFIG_PATH)
    required_keys = ["title", "subtitle", "slots", "fixedSlotVideo", "randomVideoDatabase"]
    missing = [key for key in required_keys if key not in config]
    if missing:
        raise AppError(
            bilingual(
                f"設定ファイルに不足があります: {', '.join(missing)}",
                f"Configuration is missing required fields: {', '.join(missing)}",
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    slots = int(config["slots"])
    if slots < 1:
        raise AppError(bilingual("slots は 1 以上で指定してください。", "slots must be 1 or greater."), HTTPStatus.INTERNAL_SERVER_ERROR)

    random_db = config["randomVideoDatabase"]
    if not isinstance(random_db, dict):
        raise AppError(bilingual("randomVideoDatabase の形式が不正です。", "randomVideoDatabase has an invalid format."), HTTPStatus.INTERNAL_SERVER_ERROR)

    db_missing = [key for key in ["path", "seedVideos"] if key not in random_db]
    if db_missing:
        raise AppError(
            bilingual(
                f"randomVideoDatabase に不足があります: {', '.join(db_missing)}",
                f"randomVideoDatabase is missing required fields: {', '.join(db_missing)}",
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    if not isinstance(random_db["seedVideos"], list) or not random_db["seedVideos"]:
        raise AppError(
            bilingual(
                "randomVideoDatabase.seedVideos は 1 件以上必要です。",
                "randomVideoDatabase.seedVideos must contain at least one entry.",
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return config


def load_questions(config: dict[str, Any]) -> list[dict[str, str]]:
    raw_questions = config.get("questions")
    if raw_questions is None:
        fallback_question = str(config.get("question", "")).strip()
        if not fallback_question:
            raise AppError(bilingual("questions または question を設定してください。", "Set questions or question in the configuration."), HTTPStatus.INTERNAL_SERVER_ERROR)
        raw_questions = [{"id": "q1", "text": fallback_question}]

    if not isinstance(raw_questions, list) or not raw_questions:
        raise AppError(bilingual("questions は 1 件以上の配列で指定してください。", "questions must be a non-empty array."), HTTPStatus.INTERNAL_SERVER_ERROR)

    normalized_questions: list[dict[str, str]] = []
    for index, question in enumerate(raw_questions, start=1):
        if isinstance(question, str):
            question_id = f"q{index}"
            question_text = question.strip()
        elif isinstance(question, dict):
            question_id = str(question.get("id", f"q{index}")).strip() or f"q{index}"
            question_text = str(question.get("text", "")).strip()
        else:
            raise AppError(bilingual("questions の形式が不正です。", "questions has an invalid format."), HTTPStatus.INTERNAL_SERVER_ERROR)

        if not question_text:
            raise AppError(bilingual("questions に空の設問があります。", "questions contains an empty prompt."), HTTPStatus.INTERNAL_SERVER_ERROR)

        normalized_questions.append({"id": question_id, "text": question_text})

    return normalized_questions


def get_mail_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw_settings = config.get("mailDelivery")
    if not isinstance(raw_settings, dict):
        return {"enabled": False}

    return {
        "enabled": bool(raw_settings.get("enabled", False)),
        "smtpHost": str(raw_settings.get("smtpHost", "")).strip(),
        "smtpPort": int(raw_settings.get("smtpPort", 587)),
        "useStartTls": bool(raw_settings.get("useStartTls", True)),
        "useSsl": bool(raw_settings.get("useSsl", False)),
        "username": str(raw_settings.get("username", "")).strip(),
        "passwordEnv": str(raw_settings.get("passwordEnv", "SURVEY_SMTP_PASSWORD")).strip(),
        "fromAddress": str(raw_settings.get("fromAddress", "")).strip(),
        "toAddress": str(raw_settings.get("toAddress", "")).strip(),
        "subjectPrefix": str(raw_settings.get("subjectPrefix", bilingual("アンケート結果", "Survey Result"))).strip() or bilingual("アンケート結果", "Survey Result"),
    }


def get_access_control_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw_settings = config.get("accessControl")
    if not isinstance(raw_settings, dict):
        return {"enabled": False}

    return {
        "enabled": bool(raw_settings.get("enabled", False)),
        "password": str(raw_settings.get("password", "")).strip(),
        "passwordEnv": str(raw_settings.get("passwordEnv", "SURVEY_START_PASSWORD")).strip()
        or "SURVEY_START_PASSWORD",
        "sessionTtlMinutes": max(int(raw_settings.get("sessionTtlMinutes", 720)), 1),
    }


def resolve_start_password(settings: dict[str, Any]) -> str:
    return os.environ.get(settings["passwordEnv"], "") or settings["password"]


def get_apps_script_sync_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw_settings = config.get("appsScriptSync")
    if not isinstance(raw_settings, dict):
        return {"enabled": False}

    return {
        "enabled": bool(raw_settings.get("enabled", False)),
        "endpointUrl": str(raw_settings.get("endpointUrl", "")).strip(),
        "endpointEnv": str(raw_settings.get("endpointEnv", "SURVEY_APPS_SCRIPT_ENDPOINT")).strip()
        or "SURVEY_APPS_SCRIPT_ENDPOINT",
        "token": str(raw_settings.get("token", "")).strip(),
        "tokenEnv": str(raw_settings.get("tokenEnv", "SURVEY_APPS_SCRIPT_TOKEN")).strip()
        or "SURVEY_APPS_SCRIPT_TOKEN",
        "timeoutSeconds": max(int(raw_settings.get("timeoutSeconds", 15)), 1),
    }


def get_apps_script_sync_context(config: dict[str, Any]) -> dict[str, Any]:
    settings = get_apps_script_sync_settings(config)
    if not settings["enabled"]:
        return {
            "ok": False,
            "status": "disabled",
            "message": bilingual("Apps Script 連携は無効です。", "Apps Script sync is disabled."),
            "settings": settings,
            "token": "",
        }

    endpoint_url = os.environ.get(settings["endpointEnv"], "") or settings["endpointUrl"]
    if not endpoint_url:
        return {
            "ok": False,
            "status": "invalid",
            "message": bilingual(
                f"Apps Script の endpointUrl または環境変数 {settings['endpointEnv']} が未設定です。",
                f"Apps Script endpointUrl or environment variable {settings['endpointEnv']} is not configured.",
            ),
            "settings": settings,
            "token": "",
        }

    token = os.environ.get(settings["tokenEnv"], "") or settings["token"]
    if not token:
        return {
            "ok": False,
            "status": "invalid",
            "message": bilingual(
                f"Apps Script の token または環境変数 {settings['tokenEnv']} が未設定です。",
                f"Apps Script token or environment variable {settings['tokenEnv']} is not configured.",
            ),
            "settings": settings,
            "token": "",
        }

    return {
        "ok": True,
        "status": "ready",
        "message": "",
        "settings": {**settings, "endpointUrl": endpoint_url},
        "token": token,
    }


def post_to_apps_script(*, settings: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        settings["endpointUrl"],
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=settings["timeoutSeconds"]) as response:
            body = response.read().decode("utf-8", errors="replace")
            if response.status >= 400:
                return {
                    "ok": False,
                    "error": bilingual(
                        f"Apps Script 連携に失敗しました: HTTP {response.status}",
                        f"Apps Script sync failed: HTTP {response.status}",
                    ),
                }
    except Exception as error:
        return {"ok": False, "error": bilingual(f"Apps Script 連携に失敗しました: {error}", f"Apps Script sync failed: {error}")}

    try:
        payload_json = json.loads(body or "{}")
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": bilingual("Apps Script 連携に失敗しました: JSON 応答を受け取れませんでした。", "Apps Script sync failed: JSON response was not received."),
        }

    if not isinstance(payload_json, dict):
        return {
            "ok": False,
            "error": bilingual("Apps Script 連携に失敗しました: 応答形式が不正です。", "Apps Script sync failed: response format is invalid."),
        }

    if payload_json.get("ok") is False:
        error_message = str(payload_json.get("error", "unknown error"))
        return {
            "ok": False,
            "error": bilingual(
                f"Apps Script 連携に失敗しました: {error_message}",
                f"Apps Script sync failed: {error_message}",
            ),
            "payload": payload_json,
        }

    if payload_json.get("ok") is not True:
        return {
            "ok": False,
            "error": bilingual(
                "Apps Script 連携に失敗しました: Apps Script の成功応答を確認できませんでした。",
                "Apps Script sync failed: could not confirm a successful Apps Script response.",
            ),
            "payload": payload_json,
        }

    return {"ok": True, "payload": payload_json}


def cleanup_expired_access_sessions() -> None:
    now = time.time()
    with _session_lock:
        expired_tokens = [
            token
            for token, session in _access_sessions.items()
            if float(session.get("expiresAt", 0)) <= now
        ]
        for token in expired_tokens:
            _access_sessions.pop(token, None)


def create_access_session(
    *,
    config: dict[str, Any],
    user_name: str,
    resolved_slots: list[dict[str, Any]],
    random_source: dict[str, Any],
) -> str:
    settings = get_access_control_settings(config)
    cleanup_expired_access_sessions()
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + (settings["sessionTtlMinutes"] * 60)
    with _session_lock:
        _access_sessions[token] = {
            "userName": user_name,
            "slots": json.loads(json.dumps(resolved_slots, ensure_ascii=False)),
            "randomSource": json.loads(json.dumps(random_source, ensure_ascii=False)),
            "expiresAt": expires_at,
        }
    return token


def get_access_session(token: str) -> dict[str, Any] | None:
    cleanup_expired_access_sessions()
    with _session_lock:
        session = _access_sessions.get(token)
        if session is None:
            return None
        return json.loads(json.dumps(session, ensure_ascii=False))


def update_access_session_slots(
    token: str,
    *,
    resolved_slots: list[dict[str, Any]],
    random_source: dict[str, Any],
) -> None:
    cleanup_expired_access_sessions()
    with _session_lock:
        session = _access_sessions.get(token)
        if session is None:
            return
        session["slots"] = json.loads(json.dumps(resolved_slots, ensure_ascii=False))
        session["randomSource"] = json.loads(json.dumps(random_source, ensure_ascii=False))


def require_access_session(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    settings = get_access_control_settings(config)
    if not settings["enabled"]:
        return None

    token = str(payload.get("sessionToken", "")).strip()
    if not token:
        raise AppError(
            bilingual(
                "開始セッションがありません。もう一度パスワードを入力して開始してください。",
                "No start session was found. Enter the password and start again.",
            ),
            HTTPStatus.UNAUTHORIZED,
        )

    session = get_access_session(token)
    if session is None:
        raise AppError(
            bilingual(
                "開始セッションが無効または期限切れです。もう一度パスワードを入力して開始してください。",
                "The start session is invalid or expired. Enter the password and start again.",
            ),
            HTTPStatus.UNAUTHORIZED,
        )

    return session


def get_database_path(config: dict[str, Any]) -> Path:
    database_path = (BASE_DIR / str(config["randomVideoDatabase"]["path"])).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return database_path


def ensure_random_video_database(config: dict[str, Any]) -> dict[str, Any]:
    seed_videos = [
        normalize_video_entry(video, default_source_label=DATABASE_SOURCE_LABEL)
        for video in config["randomVideoDatabase"]["seedVideos"]
    ]
    database_path = get_database_path(config)

    with _database_lock:
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS videos (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL,
                    source_label TEXT NOT NULL DEFAULT '',
                    video_group TEXT NOT NULL DEFAULT '',
                    video_code TEXT NOT NULL DEFAULT '',
                    method_name TEXT NOT NULL DEFAULT '',
                    sample_name TEXT NOT NULL DEFAULT '',
                    prompt_text TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            existing_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(videos)").fetchall()
            }
            if "video_group" not in existing_columns:
                connection.execute(
                    "ALTER TABLE videos ADD COLUMN video_group TEXT NOT NULL DEFAULT ''"
                )
            if "video_code" not in existing_columns:
                connection.execute(
                    "ALTER TABLE videos ADD COLUMN video_code TEXT NOT NULL DEFAULT ''"
                )
            if "method_name" not in existing_columns:
                connection.execute(
                    "ALTER TABLE videos ADD COLUMN method_name TEXT NOT NULL DEFAULT ''"
                )
            if "sample_name" not in existing_columns:
                connection.execute(
                    "ALTER TABLE videos ADD COLUMN sample_name TEXT NOT NULL DEFAULT ''"
                )
            if "prompt_text" not in existing_columns:
                connection.execute(
                    "ALTER TABLE videos ADD COLUMN prompt_text TEXT NOT NULL DEFAULT ''"
                )
            for video in seed_videos:
                connection.execute(
                    """
                    INSERT INTO videos (
                        id,
                        title,
                        description,
                        url,
                        source_label,
                        video_group,
                        video_code,
                        method_name,
                        sample_name,
                        prompt_text,
                        is_active,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        description = excluded.description,
                        url = excluded.url,
                        source_label = excluded.source_label,
                        video_group = excluded.video_group,
                        video_code = excluded.video_code,
                        method_name = excluded.method_name,
                        sample_name = excluded.sample_name,
                        prompt_text = excluded.prompt_text,
                        is_active = 1,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        video["id"],
                        video["title"],
                        video["description"],
                        video["url"],
                        video["sourceLabel"],
                        video["videoGroup"],
                        video["videoCode"],
                        video["methodName"],
                        video["sampleName"],
                        video["promptText"],
                    ),
                )
            active_count = int(
                connection.execute("SELECT COUNT(*) FROM videos WHERE is_active = 1").fetchone()[0]
            )

    if active_count <= 0:
        raise AppError(bilingual("データベースに有効なランダム動画がありません。", "There are no active random videos in the database."), HTTPStatus.SERVICE_UNAVAILABLE)

    return {
        "path": str(database_path.relative_to(BASE_DIR)),
        "count": active_count,
        "label": "SQLite Database",
    }


def load_random_video_catalog(config: dict[str, Any]) -> dict[str, Any]:
    database_info = ensure_random_video_database(config)
    database_path = get_database_path(config)

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                description,
                url,
                source_label,
                video_group,
                video_code,
                method_name,
                sample_name,
                prompt_text
            FROM videos
            WHERE is_active = 1
            """
        ).fetchall()

    videos = [
        {
            "id": str(row["id"]),
            "title": str(row["title"]),
            "description": str(row["description"] or ""),
            "url": str(row["url"]),
            "sourceLabel": str(row["source_label"] or DATABASE_SOURCE_LABEL),
            "videoGroup": str(row["video_group"] or ""),
            "videoCode": str(row["video_code"] or ""),
            "methodName": str(row["method_name"] or ""),
            "sampleName": str(row["sample_name"] or ""),
            "promptText": str(row["prompt_text"] or ""),
        }
        for row in rows
    ]

    if not videos:
        raise AppError(bilingual("ランダム抽選対象の動画がデータベースにありません。", "There are no videos available for random selection in the database."), HTTPStatus.SERVICE_UNAVAILABLE)

    return {"videos": videos, **database_info}


def choose_random_entries(catalog: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    if not catalog:
        raise AppError(bilingual("ランダム動画の候補がありません。", "No random video candidates are available."), HTTPStatus.SERVICE_UNAVAILABLE)
    if count <= len(catalog):
        return random.sample(catalog, count)

    chosen: list[dict[str, Any]] = []
    while len(chosen) < count:
        remaining = count - len(chosen)
        batch_size = min(remaining, len(catalog))
        chosen.extend(random.sample(catalog, batch_size))
    return chosen


def export_video_links_csv(
    *,
    fixed_video: dict[str, str],
    random_videos: list[dict[str, Any]],
) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "video_code",
        "video_url",
        "method_name",
        "sample_name",
        "prompt_text",
    ]

    catalog: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    for video in [fixed_video, *random_videos]:
        video_code = str(video.get("videoCode", "")).strip() or str(video.get("id", "")).strip()
        if not video_code or video_code in seen_codes:
            continue
        seen_codes.add(video_code)
        catalog.append(
            {
                "video_code": video_code,
                "video_url": str(video.get("url", "")).strip(),
                "method_name": str(video.get("methodName", "")).strip(),
                "sample_name": str(video.get("sampleName", "")).strip(),
                "prompt_text": str(video.get("promptText", "")).strip(),
            }
        )

    with VIDEO_LINKS_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(catalog)

    return {
        "file": VIDEO_LINKS_CSV_PATH.name,
        "rowsWritten": len(catalog),
    }


def resolve_configured_slots(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    questions = load_questions(config)
    fixed_video = normalize_video_entry(config["fixedSlotVideo"], default_source_label=bilingual("固定動画", "Fixed Video"))
    random_slot_count = max(int(config["slots"]) - 1, 0)
    random_catalog = load_random_video_catalog(config)
    export_video_links_csv(fixed_video=fixed_video, random_videos=random_catalog["videos"])
    random_videos = choose_random_entries(random_catalog["videos"], random_slot_count)

    resolved_slots = [
        {
            "slotIndex": 0,
            "slotLabel": bilingual("動画1", "Video 1"),
            "mode": "fixed",
            "modeLabel": bilingual("固定表示", "Fixed"),
            "video": fixed_video,
        }
    ]

    for offset, video in enumerate(random_videos, start=1):
        resolved_slots.append(
            {
                "slotIndex": offset,
                "slotLabel": bilingual(f"動画{offset + 1}", f"Video {offset + 1}"),
                "mode": "random",
                "modeLabel": bilingual("DBランダム", "DB Random"),
                "video": video,
            }
        )

    return {
        "slots": resolved_slots,
        "randomSource": {
            "label": random_catalog["label"],
            "hint": bilingual(
                f"{random_catalog['path']} / {random_catalog['count']} 件",
                f"{random_catalog['path']} / {random_catalog['count']} items",
            ),
            "count": random_catalog["count"],
        },
    }


def build_submission_rows(validated: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    fieldnames = [
        "user_name",
        "video_code",
        "question_text",
        "score",
    ]
    rows = [
        {
            "user_name": validated["userName"],
            "video_code": item["video"]["videoCode"],
            "question_text": item["questionText"],
            "score": item["rating"],
        }
        for item in validated["responses"]
    ]
    return fieldnames, rows


def render_csv_text(fieldnames: list[str], rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def send_submission_email(
    *,
    config: dict[str, Any],
    submission_id: str,
    user_name: str,
    attachment_name: str,
    csv_text: str,
) -> dict[str, str]:
    settings = get_mail_settings(config)
    if not settings["enabled"]:
        return {"status": "skipped", "message": bilingual("メール送信は無効です。", "Email delivery is disabled.")}

    required_fields = ["smtpHost", "smtpPort", "fromAddress", "toAddress"]
    missing = [field for field in required_fields if not settings.get(field)]
    if missing:
        return {
            "status": "skipped",
            "message": bilingual(
                f"メール設定が不足しています: {', '.join(missing)}",
                f"Email settings are missing: {', '.join(missing)}",
            ),
        }

    password = ""
    if settings["username"]:
        password = os.environ.get(settings["passwordEnv"], "")
        if not password:
            return {
                "status": "skipped",
                "message": bilingual(
                    f"環境変数 {settings['passwordEnv']} が未設定です。",
                    f"Environment variable {settings['passwordEnv']} is not configured.",
                ),
            }

    message = EmailMessage()
    message["Subject"] = f"{settings['subjectPrefix']} {submission_id}"
    message["From"] = settings["fromAddress"]
    message["To"] = settings["toAddress"]
    message.set_content(
        "\n".join(
            [
                bilingual("アンケート結果を送付します。", "Survey results are attached."),
                f"Submission ID: {submission_id}",
                bilingual(f"User名: {user_name or '(未入力)'}", f"User Name: {user_name or '(blank)'}"),
                "",
                bilingual("CSV を添付しています。", "The CSV file is attached."),
            ]
        )
    )
    message.add_attachment(
        csv_text.encode("utf-8-sig"),
        maintype="text",
        subtype="csv",
        filename=attachment_name,
    )

    try:
        if settings["useSsl"]:
            with smtplib.SMTP_SSL(settings["smtpHost"], settings["smtpPort"], timeout=30) as smtp:
                if settings["username"]:
                    smtp.login(settings["username"], password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings["smtpHost"], settings["smtpPort"], timeout=30) as smtp:
                if settings["useStartTls"]:
                    smtp.starttls()
                if settings["username"]:
                    smtp.login(settings["username"], password)
                smtp.send_message(message)
    except Exception as error:
        return {"status": "failed", "message": bilingual(f"メール送信に失敗しました: {error}", f"Email delivery failed: {error}")}

    return {"status": "sent", "message": bilingual(f"{settings['toAddress']} へメール送信しました。", f"Email was sent to {settings['toAddress']}.")}


def sync_submission_to_apps_script(
    *,
    config: dict[str, Any],
    submission_id: str,
    rows: list[dict[str, Any]],
) -> dict[str, str]:
    context = get_apps_script_sync_context(config)
    if not context["ok"]:
        return {
            "status": "skipped",
            "message": context["message"],
        }

    result = post_to_apps_script(
        settings=context["settings"],
        payload={
            "token": context["token"],
            "submission_id": submission_id,
            "rows": rows,
        },
    )
    if not result["ok"]:
        return {"status": "failed", "message": result["error"]}

    payload_json = result.get("payload", {})
    written = payload_json.get("written")
    if written is None:
        return {"status": "sent", "message": bilingual("Apps Script へ送信しました。", "Sent to Apps Script.")}
    return {"status": "sent", "message": bilingual(f"Apps Script へ {written} 行送信しました。", f"Sent {written} rows to Apps Script.")}


def diagnose_google_sheet_write_access(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    context = get_apps_script_sync_context(config)
    if not context["ok"]:
        if context["status"] == "disabled":
            return {
                "ok": True,
                "status": "disabled",
                "message": bilingual(
                    "Google Sheets 連携は無効です。ローカル保存のみで開始できます。",
                    "Google Sheets sync is disabled. The survey can start with local saving only.",
                ),
            }
        return {
            "ok": False,
            "status": "failed",
            "message": context["message"],
        }

    result = post_to_apps_script(
        settings=context["settings"],
        payload={
            "token": context["token"],
            "submission_id": f"HEALTHCHECK-{int(time.time())}",
            "rows": [],
            "diagnostic": True,
        },
    )
    if not result["ok"]:
        return {
            "ok": False,
            "status": "failed",
            "message": result["error"],
        }

    payload_json = result.get("payload", {})
    return {
        "ok": True,
        "status": "ready",
        "message": bilingual("Google Sheets への保存確認が完了しました。", "Google Sheets availability check completed."),
        "detail": {
            "written": int(payload_json.get("written", 0) or 0),
        },
    }


def start_survey_session(payload: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    settings = get_access_control_settings(config)
    user_name = str(payload.get("userName", "")).strip()
    if not user_name:
        raise AppError(bilingual("User名は必須です。", "User name is required."))

    if settings["enabled"]:
        configured_password = resolve_start_password(settings)
        if not configured_password:
            raise AppError(
                bilingual(
                    "開始パスワードがサーバーに設定されていません。",
                    "The start password is not configured on the server.",
                ),
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        submitted_password = str(payload.get("startPassword", ""))
        if not hmac.compare_digest(submitted_password, configured_password):
            raise AppError(
                bilingual(
                    "開始パスワードが正しくありません。",
                    "The start password is incorrect.",
                ),
                HTTPStatus.UNAUTHORIZED,
            )

    readiness = diagnose_google_sheet_write_access(config)
    if not readiness["ok"]:
        raise AppError(readiness["message"], HTTPStatus.SERVICE_UNAVAILABLE)

    resolved = resolve_configured_slots(config)
    session_token = create_access_session(
        config=config,
        user_name=user_name,
        resolved_slots=resolved["slots"],
        random_source=resolved["randomSource"],
    )

    return {
        "message": readiness["message"],
        "status": readiness["status"],
        "sessionToken": session_token,
        "randomSource": resolved["randomSource"],
        "slots": resolved["slots"],
    }


def build_bootstrap_payload() -> dict[str, Any]:
    config = load_config()
    questions = load_questions(config)
    access_control = get_access_control_settings(config)

    return {
        "title": config["title"],
        "subtitle": config["subtitle"],
        "slots": int(config["slots"]),
        "questions": questions,
        "scaleLabels": list(config.get("scaleLabels", ["1", "2", "3", "4", "5"])),
        "scaleHints": list(
            config.get(
                "scaleHints",
                [
                    bilingual("とても低い", "Very low"),
                    bilingual("やや低い", "Somewhat low"),
                    bilingual("普通", "Neutral"),
                    bilingual("やや高い", "Somewhat high"),
                    bilingual("とても高い", "Very high"),
                ],
            )
        ),
        "assignmentSummary": bilingual(
            "中央の質問に対して6本すべてを評価し、次の質問へ進みます。最後の設問で送信すると動画2〜6が自動で次のセットに入れ替わります。",
            "Rate all six videos for the question in the center and proceed to the next question. After submitting the final question, Videos 2 to 6 automatically switch to the next set.",
        ),
        "randomSource": {
            "label": bilingual("開始後に読み込み", "Loads after start"),
            "hint": bilingual(
                "回答開始後に動画セットと取得元が表示されます。",
                "The video set and source will appear after the survey starts.",
            ),
            "count": 0,
        },
        "slotsResolved": [],
        "accessControl": {
            "enabled": bool(access_control["enabled"]),
            "sessionTtlMinutes": int(access_control.get("sessionTtlMinutes", 720)),
        },
    }


def validate_submission(payload: dict[str, Any], session: dict[str, Any] | None = None) -> dict[str, Any]:
    config = load_config()
    questions = load_questions(config)
    question_map = {question["id"]: question["text"] for question in questions}
    user_name = str(payload.get("userName", "")).strip()
    if not user_name:
        raise AppError(bilingual("User名は必須です。", "User name is required."))
    if len(user_name) > 80:
        raise AppError(bilingual("User名は 80 文字以内で入力してください。", "User name must be 80 characters or fewer."))
    if session is not None and user_name != str(session.get("userName", "")).strip():
        raise AppError(
            bilingual(
                "開始時のUser名と送信時のUser名が一致しません。",
                "The user name at submission does not match the name used at start.",
            )
        )
    responses = payload.get("responses")
    if not isinstance(responses, list) or not responses:
        raise AppError(bilingual("送信データに回答情報がありません。", "The submission payload does not contain any responses."))
    session_slots = {
        int(slot["slotIndex"]): slot
        for slot in (session or {}).get("slots", [])
        if isinstance(slot, dict) and "slotIndex" in slot
    }

    expected_responses = int(config["slots"]) * len(questions)
    if len(responses) != expected_responses:
        raise AppError(
            bilingual(
                f"回答数が不正です。{len(questions)} 問 x {config['slots']} 本の合計 {expected_responses} 件必要です。",
                f"Invalid number of responses. {expected_responses} responses are required for {len(questions)} questions x {config['slots']} videos.",
            )
        )

    validated_responses: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, int]] = set()
    for response in responses:
        try:
            slot_index = int(response["slotIndex"])
            question_index = int(response["questionIndex"])
            rating = int(response["rating"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError(bilingual("評価は 1 から 5 の整数で指定してください。", "Ratings must be integers from 1 to 5.")) from exc
        if rating < 1 or rating > 5:
            raise AppError(bilingual("評価は 1 から 5 の範囲で指定してください。", "Ratings must be between 1 and 5."))
        if slot_index < 0 or slot_index >= int(config["slots"]):
            raise AppError(bilingual("不正な動画番号が送信されました。", "An invalid video index was submitted."))
        if question_index < 0 or question_index >= len(questions):
            raise AppError(bilingual("不正な設問番号が送信されました。", "An invalid question index was submitted."))

        question_id = str(response.get("questionId", "")).strip()
        if question_id not in question_map:
            raise AppError(bilingual("不正な設問IDが送信されました。", "An invalid question ID was submitted."))

        seen_key = (question_id, slot_index)
        if seen_key in seen_pairs:
            raise AppError(bilingual("同じ設問・動画の回答が重複しています。", "Duplicate responses were submitted for the same question and video."))
        seen_pairs.add(seen_key)

        session_slot = session_slots.get(slot_index)
        if session is not None and session_slot is None:
            raise AppError(
                bilingual(
                    "開始セッションに存在しない動画枠が送信されました。",
                    "A video slot not present in the start session was submitted.",
                )
            )

        video = response.get("video")
        if not isinstance(video, dict) or not video.get("title") or not video.get("url"):
            raise AppError(bilingual("動画情報が不足しています。", "Video information is missing."))

        if session_slot is not None:
            expected_video = session_slot.get("video", {})
            if str(video.get("id", "")) != str(expected_video.get("id", "")):
                raise AppError(
                    bilingual(
                        f"{slot_index + 1} 番の動画情報が開始時の内容と一致しません。",
                        f"Video information for slot {slot_index + 1} does not match the started session.",
                    )
                )
            normalized_slot_label = str(session_slot.get("slotLabel", ""))
            normalized_mode = str(session_slot.get("mode", ""))
            normalized_mode_label = str(session_slot.get("modeLabel", ""))
            normalized_video = {
                "id": str(expected_video.get("id", "")),
                "title": str(expected_video.get("title", "")),
                "url": str(expected_video.get("url", "")),
                "sourceLabel": str(expected_video.get("sourceLabel", "")),
                "description": str(expected_video.get("description", "")),
                "videoGroup": str(expected_video.get("videoGroup", "")).strip(),
                "videoCode": str(expected_video.get("videoCode", "")).strip(),
                "methodName": str(expected_video.get("methodName", "")).strip(),
                "sampleName": str(expected_video.get("sampleName", "")).strip(),
                "promptText": str(expected_video.get("promptText", "")).strip(),
            }
        else:
            normalized_slot_label = str(
                response.get("slotLabel", bilingual(f"動画{slot_index + 1}", f"Video {slot_index + 1}"))
            )
            normalized_mode = str(response.get("mode", "random"))
            normalized_mode_label = str(response.get("modeLabel", ""))
            normalized_video = {
                "id": str(video.get("id", "")),
                "title": str(video["title"]),
                "url": str(video["url"]),
                "sourceLabel": str(video.get("sourceLabel", "")),
                "description": str(video.get("description", "")),
                "videoGroup": str(video.get("videoGroup", "")).strip(),
                "videoCode": str(video.get("videoCode", "")).strip(),
                "methodName": str(video.get("methodName", "")).strip(),
                "sampleName": str(video.get("sampleName", "")).strip(),
                "promptText": str(video.get("promptText", "")).strip(),
            }

        validated_responses.append(
            {
                "questionId": question_id,
                "questionIndex": question_index,
                "questionText": question_map[question_id],
                "slotIndex": slot_index,
                "slotLabel": normalized_slot_label,
                "mode": normalized_mode,
                "modeLabel": normalized_mode_label,
                "rating": rating,
                "video": normalized_video,
            }
        )

    if len(seen_pairs) != expected_responses:
        raise AppError(bilingual("未回答の設問があります。すべての動画を評価してください。", "Some questions are unanswered. Please rate every video."))

    validated_responses.sort(key=lambda item: (item["questionIndex"], item["slotIndex"]))
    return {
        "userName": user_name,
        "submittedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "responses": validated_responses,
        "summary": {
            "averageRating": round(
                sum(item["rating"] for item in validated_responses) / len(validated_responses),
                2,
            )
        },
    }


def save_submission_csv(payload: dict[str, Any], *, client_ip: str, user_agent: str) -> dict[str, Any]:
    del client_ip, user_agent
    config = load_config()
    session = require_access_session(payload, config)
    validated = validate_submission(payload, session=session)
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)

    submission_id = f"SUB-{time.strftime('%Y%m%d-%H%M%S')}-{random.randint(1000, 9999)}"
    fieldnames, rows = build_submission_rows(validated)
    csv_text = render_csv_text(fieldnames, rows)
    download_filename = f"{submission_id}.csv"

    with _csv_lock:
        file_exists = prepare_csv_results_file(fieldnames)
        with CSV_RESULTS_PATH.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerows(rows)

    per_submission_dir = RESPONSES_DIR / "submissions"
    per_submission_dir.mkdir(parents=True, exist_ok=True)
    (per_submission_dir / download_filename).write_text(csv_text, encoding="utf-8-sig", newline="")

    mail_result = send_submission_email(
        config=config,
        submission_id=submission_id,
        user_name=validated["userName"],
        attachment_name=download_filename,
        csv_text=csv_text,
    )
    apps_script_result = sync_submission_to_apps_script(
        config=config,
        submission_id=submission_id,
        rows=rows,
    )

    return {
        "file": CSV_RESULTS_PATH.name,
        "submissionId": submission_id,
        "rowsWritten": len(validated["responses"]),
        "downloadFilename": download_filename,
        "submissionCsv": csv_text,
        "mailStatus": mail_result["status"],
        "mailMessage": mail_result["message"],
        "appsScriptStatus": apps_script_result["status"],
        "appsScriptMessage": apps_script_result["message"],
    }


def prepare_csv_results_file(fieldnames: list[str]) -> bool:
    if not CSV_RESULTS_PATH.exists():
        return False

    with CSV_RESULTS_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        existing_header = next(reader, [])

    if existing_header == fieldnames:
        return True

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    stem = CSV_RESULTS_PATH.stem
    suffix = CSV_RESULTS_PATH.suffix
    for index in count():
        candidate_name = (
            f"{stem}_legacy_{timestamp}{suffix}"
            if index == 0
            else f"{stem}_legacy_{timestamp}_{index}{suffix}"
        )
        candidate_path = CSV_RESULTS_PATH.with_name(candidate_name)
        if not candidate_path.exists():
            CSV_RESULTS_PATH.rename(candidate_path)
            return False

    return False


class SurveyRequestHandler(BaseHTTPRequestHandler):
    server_version = "VideoSurvey/2.0"

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self.serve_static("index.html")
                return
            if parsed.path == "/api/bootstrap":
                self.respond_json(build_bootstrap_payload())
                return
            if parsed.path.startswith("/static/"):
                self.serve_static(parsed.path.removeprefix("/static/"))
                return
            self.respond_error(HTTPStatus.NOT_FOUND, bilingual("ページが見つかりません。", "Page not found."))
        except AppError as exc:
            self.respond_error(exc.status, exc.message)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self.read_json_body()
            if parsed.path == "/api/start-session":
                result = start_survey_session(payload)
                self.respond_json(
                    {
                        "ok": True,
                        "message": result["message"],
                        "status": result["status"],
                        "sessionToken": result["sessionToken"],
                        "randomSource": result["randomSource"],
                        "slots": result["slots"],
                    }
                )
                return
            if parsed.path == "/api/resolve-slots":
                config = load_config()
                session = require_access_session(payload, config)
                resolved = resolve_configured_slots(config)
                if session is not None:
                    update_access_session_slots(
                        str(payload.get("sessionToken", "")).strip(),
                        resolved_slots=resolved["slots"],
                        random_source=resolved["randomSource"],
                    )
                self.respond_json(resolved)
                return
            if parsed.path == "/api/submissions":
                result = save_submission_csv(
                    payload,
                    client_ip=self.client_address[0],
                    user_agent=self.headers.get("User-Agent", ""),
                )
                self.respond_json(
                    {
                        "ok": True,
                        "message": bilingual("回答をCSVに保存しました。", "Responses were saved to CSV."),
                        "file": result["file"],
                        "submissionId": result["submissionId"],
                        "rowsWritten": result["rowsWritten"],
                        "downloadFilename": result["downloadFilename"],
                        "submissionCsv": result["submissionCsv"],
                        "mailStatus": result["mailStatus"],
                        "mailMessage": result["mailMessage"],
                        "appsScriptStatus": result["appsScriptStatus"],
                        "appsScriptMessage": result["appsScriptMessage"],
                    },
                    status=HTTPStatus.CREATED,
                )
                return
            self.respond_error(HTTPStatus.NOT_FOUND, bilingual("API が見つかりません。", "API not found."))
        except AppError as exc:
            self.respond_error(exc.status, exc.message)
        except json.JSONDecodeError:
            self.respond_error(HTTPStatus.BAD_REQUEST, bilingual("JSON の解析に失敗しました。", "Failed to parse JSON."))

    def read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            raise AppError(bilingual("リクエスト本文が空です。", "The request body is empty."))
        raw = self.rfile.read(content_length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise AppError(bilingual("JSON オブジェクトを送信してください。", "Send a JSON object in the request body."))
        return payload

    def serve_static(self, relative_path: str) -> None:
        requested = (STATIC_DIR / relative_path).resolve()
        try:
            requested.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.respond_error(HTTPStatus.FORBIDDEN, bilingual("許可されていないパスです。", "This path is not allowed."))
            return

        if not requested.exists() or not requested.is_file():
            self.respond_error(HTTPStatus.NOT_FOUND, bilingual("ファイルが見つかりません。", "File not found."))
            return

        mime_type, _ = mimetypes.guess_type(str(requested))
        body = requested.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_json(self, payload: dict[str, Any], *, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_error(self, status: int, message: str) -> None:
        self.respond_json({"ok": False, "error": message}, status=status)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Video survey application server")
    default_port = int(os.environ.get("PORT", "8000"))
    default_host = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", default=default_port, type=int)
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), SurveyRequestHandler)
    print(f"Video survey server running at http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
