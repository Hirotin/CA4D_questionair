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
import re
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


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{str(key): str(value or "") for key, value in row.items()} for row in reader]


def slugify_identifier(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", str(value).strip()).strip("-").lower()
    return normalized or "video"


def choose_catalog_identifier(row: dict[str, str]) -> str:
    for key in ("id", "video_code", "object_key", "video_url", "title"):
        candidate = str(row.get(key, "")).strip()
        if candidate:
            return slugify_identifier(candidate)
    raise AppError(
        bilingual(
            "CSV 行に id / video_code / object_key / video_url / title がありません。",
            "CSV row is missing id / video_code / object_key / video_url / title.",
        ),
        HTTPStatus.INTERNAL_SERVER_ERROR,
    )


def get_video_storage_settings(config: dict[str, Any]) -> dict[str, str]:
    raw_settings = config.get("videoStorage")
    if not isinstance(raw_settings, dict):
        raw_settings = {}

    public_base_url_env = (
        str(raw_settings.get("publicBaseUrlEnv", "SURVEY_VIDEO_PUBLIC_BASE_URL")).strip()
        or "SURVEY_VIDEO_PUBLIC_BASE_URL"
    )
    public_base_url = os.environ.get(public_base_url_env, "").strip() or str(
        raw_settings.get("publicBaseUrl", "")
    ).strip()

    return {
        "provider": str(raw_settings.get("provider", "")).strip(),
        "publicBaseUrl": public_base_url,
        "publicBaseUrlEnv": public_base_url_env,
    }


def build_public_video_url(public_base_url: str, object_key: str) -> str:
    normalized_base_url = str(public_base_url).strip().rstrip("/")
    normalized_object_key = str(object_key).strip().lstrip("/")
    if not normalized_base_url or not normalized_object_key:
        return ""
    encoded_key = urllib.parse.quote(normalized_object_key, safe="/-_.~")
    return f"{normalized_base_url}/{encoded_key}"


def resolve_video_location(
    entry: dict[str, Any], *, public_base_url: str
) -> tuple[str, str]:
    raw_url = str(entry.get("url", "")).strip()
    object_key = str(entry.get("objectKey", "")).strip().lstrip("/")

    if object_key:
        resolved_url = build_public_video_url(public_base_url, object_key)
        if not resolved_url:
            raise AppError(
                bilingual(
                    "objectKey を使う場合は videoStorage.publicBaseUrl または SURVEY_VIDEO_PUBLIC_BASE_URL を設定してください。",
                    "When objectKey is used, set videoStorage.publicBaseUrl or SURVEY_VIDEO_PUBLIC_BASE_URL.",
                ),
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        return object_key, resolved_url

    if raw_url:
        parsed_url = urllib.parse.urlparse(raw_url)
        if public_base_url and not parsed_url.scheme and not raw_url.startswith("/") and not raw_url.startswith("//"):
            object_key = raw_url.lstrip("/")
            return object_key, build_public_video_url(public_base_url, object_key)
        return "", raw_url

    return "", ""


def normalize_video_entry(
    entry: dict[str, Any], *, default_source_label: str, public_base_url: str = ""
) -> dict[str, str]:
    required_keys = ["id", "title"]
    missing = [key for key in required_keys if key not in entry]
    if missing:
        raise AppError(
            bilingual(
                f"動画エントリに不足があります: {', '.join(missing)}",
                f"Video entry is missing required fields: {', '.join(missing)}",
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    object_key, resolved_url = resolve_video_location(
        entry, public_base_url=public_base_url
    )
    if not resolved_url:
        raise AppError(
            bilingual(
                "動画エントリには url または objectKey が必要です。",
                "Video entry requires either url or objectKey.",
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return {
        "id": str(entry["id"]),
        "title": str(entry["title"]),
        "url": resolved_url,
        "objectKey": object_key,
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
    required_keys = ["title", "subtitle", "slots", "randomVideoDatabase"]
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
    if not isinstance(random_db["seedVideos"], list):
        raise AppError(
            bilingual(
                "randomVideoDatabase.seedVideos は配列で指定してください。",
                "randomVideoDatabase.seedVideos must be an array.",
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    catalog_csv_paths = random_db.get("catalogCsvPaths", [])
    if catalog_csv_paths is None:
        catalog_csv_paths = []
    if not isinstance(catalog_csv_paths, list):
        raise AppError(
            bilingual(
                "randomVideoDatabase.catalogCsvPaths は配列で指定してください。",
                "randomVideoDatabase.catalogCsvPaths must be an array.",
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )
    if not random_db["seedVideos"] and not catalog_csv_paths:
        raise AppError(
            bilingual(
                "randomVideoDatabase.seedVideos か randomVideoDatabase.catalogCsvPaths のどちらかに 1 件以上必要です。",
                "At least one entry is required in randomVideoDatabase.seedVideos or randomVideoDatabase.catalogCsvPaths.",
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return config


def normalize_shape_id(value: str) -> str:
    text = str(value).strip()
    match = re.search(r"(\d{3,})", text)
    return match.group(1) if match else text


def normalize_method_code(value: str) -> str:
    text = str(value).strip()
    match = re.search(r"(\d{4})", text)
    return match.group(1) if match else text


def load_shape_survey_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw_settings = config.get("shapeSurvey")
    if not isinstance(raw_settings, dict):
        raw_settings = {}

    raw_shape_order = raw_settings.get("shapeOrder", [])
    if raw_shape_order is None:
        raw_shape_order = []
    if not isinstance(raw_shape_order, list):
        raise AppError(
            bilingual(
                "shapeSurvey.shapeOrder は配列で指定してください。",
                "shapeSurvey.shapeOrder must be an array.",
            ),
            HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return {
        "shapeOrder": [normalize_shape_id(item) for item in raw_shape_order if str(item).strip()],
        "referenceMethodCode": normalize_method_code(raw_settings.get("referenceMethodCode", "0011")),
        "referenceSlotLabel": str(
            raw_settings.get("referenceSlotLabel", bilingual("動画0", "Video 0"))
        ).strip()
        or bilingual("動画0", "Video 0"),
    }


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

    endpoint_env_name = (
        str(raw_settings.get("endpointEnv", "SURVEY_APPS_SCRIPT_ENDPOINT")).strip()
        or "SURVEY_APPS_SCRIPT_ENDPOINT"
    )
    token_env_name = (
        str(raw_settings.get("tokenEnv", "SURVEY_APPS_SCRIPT_TOKEN")).strip()
        or "SURVEY_APPS_SCRIPT_TOKEN"
    )
    sheet_url_env_name = (
        str(raw_settings.get("sheetUrlEnv", "SURVEY_GOOGLE_SHEET_URL")).strip()
        or "SURVEY_GOOGLE_SHEET_URL"
    )
    enabled_env_name = (
        str(raw_settings.get("enabledEnv", "SURVEY_APPS_SCRIPT_ENABLED")).strip()
        or "SURVEY_APPS_SCRIPT_ENABLED"
    )
    endpoint_env_value = os.environ.get(endpoint_env_name, "").strip()
    token_env_value = os.environ.get(token_env_name, "").strip()
    enabled_env_value = os.environ.get(enabled_env_name, "").strip().lower()
    env_requested_enable = enabled_env_value in {"1", "true", "yes", "on"}
    env_sync_present = bool(endpoint_env_value or token_env_value or enabled_env_value)

    return {
        "enabled": bool(raw_settings.get("enabled", False) or env_requested_enable or env_sync_present),
        "endpointUrl": str(raw_settings.get("endpointUrl", "")).strip(),
        "endpointEnv": endpoint_env_name,
        "token": str(raw_settings.get("token", "")).strip(),
        "tokenEnv": token_env_name,
        "sheetUrl": str(os.environ.get(sheet_url_env_name, "") or raw_settings.get("sheetUrl", "")).strip(),
        "sheetUrlEnv": sheet_url_env_name,
        "enabledEnv": enabled_env_name,
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


def get_results_r2_archive_settings(config: dict[str, Any]) -> dict[str, Any]:
    raw_settings = config.get("resultsR2Archive")
    if not isinstance(raw_settings, dict):
        return {"enabled": False}

    bucket_env_name = (
        str(raw_settings.get("bucketEnv", "R2_BUCKET_NAME")).strip()
        or "R2_BUCKET_NAME"
    )
    endpoint_env_name = (
        str(raw_settings.get("endpointEnv", "R2_ENDPOINT_URL")).strip()
        or "R2_ENDPOINT_URL"
    )
    access_key_env_name = (
        str(raw_settings.get("accessKeyEnv", "AWS_ACCESS_KEY_ID")).strip()
        or "AWS_ACCESS_KEY_ID"
    )
    secret_key_env_name = (
        str(raw_settings.get("secretKeyEnv", "AWS_SECRET_ACCESS_KEY")).strip()
        or "AWS_SECRET_ACCESS_KEY"
    )
    session_token_env_name = (
        str(raw_settings.get("sessionTokenEnv", "AWS_SESSION_TOKEN")).strip()
        or "AWS_SESSION_TOKEN"
    )
    prefix_env_name = (
        str(raw_settings.get("prefixEnv", "SURVEY_RESULTS_R2_PREFIX")).strip()
        or "SURVEY_RESULTS_R2_PREFIX"
    )
    enabled_env_name = (
        str(raw_settings.get("enabledEnv", "SURVEY_RESULTS_R2_ENABLED")).strip()
        or "SURVEY_RESULTS_R2_ENABLED"
    )

    bucket_env_value = os.environ.get(bucket_env_name, "").strip()
    endpoint_env_value = os.environ.get(endpoint_env_name, "").strip()
    access_key_env_value = os.environ.get(access_key_env_name, "").strip()
    secret_key_env_value = os.environ.get(secret_key_env_name, "").strip()
    enabled_env_value = os.environ.get(enabled_env_name, "").strip().lower()
    env_requested_enable = enabled_env_value in {"1", "true", "yes", "on"}
    env_archive_present = bool(
        bucket_env_value
        or endpoint_env_value
        or access_key_env_value
        or secret_key_env_value
        or enabled_env_value
    )

    return {
        "enabled": bool(raw_settings.get("enabled", False) or env_requested_enable or env_archive_present),
        "bucket": str(raw_settings.get("bucket", "")).strip(),
        "bucketEnv": bucket_env_name,
        "endpointUrl": str(raw_settings.get("endpointUrl", "")).strip(),
        "endpointEnv": endpoint_env_name,
        "accessKey": str(raw_settings.get("accessKey", "")).strip(),
        "accessKeyEnv": access_key_env_name,
        "secretKey": str(raw_settings.get("secretKey", "")).strip(),
        "secretKeyEnv": secret_key_env_name,
        "sessionToken": str(raw_settings.get("sessionToken", "")).strip(),
        "sessionTokenEnv": session_token_env_name,
        "prefix": str(raw_settings.get("prefix", "survey-results")).strip().strip("/"),
        "prefixEnv": prefix_env_name,
        "regionName": str(raw_settings.get("regionName", "auto")).strip() or "auto",
        "enabledEnv": enabled_env_name,
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
    shape_rounds: list[dict[str, Any]],
    random_source: dict[str, Any],
) -> str:
    settings = get_access_control_settings(config)
    cleanup_expired_access_sessions()
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + (settings["sessionTtlMinutes"] * 60)
    with _session_lock:
        _access_sessions[token] = {
            "userName": user_name,
            "shapeRounds": json.loads(json.dumps(shape_rounds, ensure_ascii=False)),
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
    shape_rounds: list[dict[str, Any]],
    random_source: dict[str, Any],
) -> None:
    cleanup_expired_access_sessions()
    with _session_lock:
        session = _access_sessions.get(token)
        if session is None:
            return
        session["shapeRounds"] = json.loads(json.dumps(shape_rounds, ensure_ascii=False))
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


def get_catalog_csv_paths(config: dict[str, Any]) -> list[Path]:
    raw_paths = config["randomVideoDatabase"].get("catalogCsvPaths", [])
    return [(BASE_DIR / str(path)).resolve() for path in raw_paths]


def normalize_catalog_csv_row(
    raw_row: dict[str, str], *, public_base_url: str, default_source_label: str
) -> dict[str, str]:
    fallback_identifier = choose_catalog_identifier(raw_row)
    entry = {
        "id": str(raw_row.get("id", "")).strip() or fallback_identifier,
        "title": str(raw_row.get("title", "")).strip()
        or str(raw_row.get("sample_name", "")).strip()
        or str(raw_row.get("video_code", "")).strip()
        or fallback_identifier,
        "description": str(raw_row.get("description", "")).strip(),
        "objectKey": str(raw_row.get("object_key", "")).strip(),
        "url": str(raw_row.get("video_url", "")).strip(),
        "sourceLabel": str(raw_row.get("source_label", "")).strip() or default_source_label,
        "videoGroup": str(raw_row.get("video_group", "")).strip(),
        "videoCode": str(raw_row.get("video_code", "")).strip() or fallback_identifier.upper(),
        "methodName": str(raw_row.get("method_name", "")).strip(),
        "sampleName": str(raw_row.get("sample_name", "")).strip(),
        "promptText": str(raw_row.get("prompt_text", "")).strip(),
    }
    return normalize_video_entry(
        entry,
        default_source_label=default_source_label,
        public_base_url=public_base_url,
    )


def load_catalog_csv_seed_videos(config: dict[str, Any]) -> list[dict[str, str]]:
    storage_settings = get_video_storage_settings(config)
    videos: list[dict[str, str]] = []
    for csv_path in get_catalog_csv_paths(config):
        if not csv_path.exists():
            raise AppError(
                bilingual(
                    f"動画カタログ CSV が見つかりません: {csv_path}",
                    f"Video catalog CSV was not found: {csv_path}",
                ),
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        for raw_row in load_csv_rows(csv_path):
            videos.append(
                normalize_catalog_csv_row(
                    raw_row,
                    public_base_url=storage_settings["publicBaseUrl"],
                    default_source_label=DATABASE_SOURCE_LABEL,
                )
            )
    return videos


def ensure_random_video_database(config: dict[str, Any]) -> dict[str, Any]:
    storage_settings = get_video_storage_settings(config)
    seed_videos = [
        normalize_video_entry(
            video,
            default_source_label=DATABASE_SOURCE_LABEL,
            public_base_url=storage_settings["publicBaseUrl"],
        )
        for video in config["randomVideoDatabase"]["seedVideos"]
    ]
    seed_videos.extend(load_catalog_csv_seed_videos(config))
    database_path = get_database_path(config)

    with _database_lock:
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS videos (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    object_key TEXT NOT NULL DEFAULT '',
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
            if "object_key" not in existing_columns:
                connection.execute(
                    "ALTER TABLE videos ADD COLUMN object_key TEXT NOT NULL DEFAULT ''"
                )
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
                        object_key,
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
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        description = excluded.description,
                        object_key = excluded.object_key,
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
                        video["objectKey"],
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
                object_key,
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
            "objectKey": str(row["object_key"] or ""),
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


def export_video_links_csv(*, videos: list[dict[str, Any]]) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "video_code",
        "object_key",
        "video_url",
        "method_name",
        "sample_name",
        "prompt_text",
    ]

    catalog: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    for video in videos:
        video_code = str(video.get("videoCode", "")).strip() or str(video.get("id", "")).strip()
        if not video_code or video_code in seen_codes:
            continue
        seen_codes.add(video_code)
        catalog.append(
            {
                "video_code": video_code,
                "object_key": str(video.get("objectKey", "")).strip(),
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


def sort_shape_ids(shape_ids: list[str], preferred_order: list[str]) -> list[str]:
    if not preferred_order:
        return sorted(shape_ids, key=lambda item: (int(item) if item.isdigit() else item))

    preferred_index = {value: index for index, value in enumerate(preferred_order)}
    return sorted(
        shape_ids,
        key=lambda item: (
            preferred_index.get(item, len(preferred_order)),
            int(item) if item.isdigit() else item,
        ),
    )


def build_shape_rounds(config: dict[str, Any]) -> dict[str, Any]:
    config = config or load_config()
    random_catalog = load_random_video_catalog(config)
    export_video_links_csv(videos=random_catalog["videos"])

    shape_settings = load_shape_survey_settings(config)
    expected_slots = int(config["slots"])
    videos_by_shape: dict[str, list[dict[str, Any]]] = {}
    for video in random_catalog["videos"]:
        shape_id = normalize_shape_id(video.get("sampleName", ""))
        if not shape_id:
            raise AppError(
                bilingual(
                    f"動画 {video.get('videoCode', video.get('id', ''))} に sample_name がありません。",
                    f"Video {video.get('videoCode', video.get('id', ''))} is missing sample_name.",
                ),
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        videos_by_shape.setdefault(shape_id, []).append(video)

    shape_order = sort_shape_ids(list(videos_by_shape.keys()), shape_settings["shapeOrder"])
    shape_rounds: list[dict[str, Any]] = []
    for shape_index, shape_id in enumerate(shape_order):
        shape_videos = list(videos_by_shape[shape_id])
        if len(shape_videos) != expected_slots:
            raise AppError(
                bilingual(
                    f"形状 {shape_id} の動画数が {expected_slots} 本ではありません。現在 {len(shape_videos)} 本です。",
                    f"Shape {shape_id} does not have {expected_slots} videos. It currently has {len(shape_videos)} videos.",
                ),
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        reference_video = None
        for video in shape_videos:
            if normalize_method_code(video.get("methodName", "")) == shape_settings["referenceMethodCode"]:
                reference_video = video
                break
        if reference_video is None:
            raise AppError(
                bilingual(
                    f"形状 {shape_id} に参照用手法 {shape_settings['referenceMethodCode']} がありません。",
                    f"Shape {shape_id} is missing reference method {shape_settings['referenceMethodCode']}.",
                ),
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        shuffled_videos = random.sample(shape_videos, len(shape_videos))
        slots: list[dict[str, Any]] = []
        reference_slot_index = 0
        for slot_index, video in enumerate(shuffled_videos):
            if str(video.get("id", "")) == str(reference_video.get("id", "")):
                reference_slot_index = slot_index
            slots.append(
                {
                    "slotIndex": slot_index,
                    "slotLabel": bilingual(f"動画{slot_index + 1}", f"Video {slot_index + 1}"),
                    "mode": "shape-batch",
                    "modeLabel": bilingual("同一形状セット", "Same-shape set"),
                    "video": video,
                }
            )

        shape_rounds.append(
            {
                "shapeIndex": shape_index,
                "shapeId": shape_id,
                "shapeLabel": bilingual(f"形状 {shape_id}", f"Shape {shape_id}"),
                "referenceSlotIndex": reference_slot_index,
                "referenceSlotLabel": shape_settings["referenceSlotLabel"],
                "referenceMethodCode": shape_settings["referenceMethodCode"],
                "referenceVideo": reference_video,
                "slots": slots,
            }
        )

    return {
        "shapeRounds": shape_rounds,
        "slots": shape_rounds[0]["slots"] if shape_rounds else [],
        "randomSource": {
            "label": random_catalog["label"],
            "hint": bilingual(
                f"{random_catalog['path']} / {len(shape_rounds)} 形状 x {expected_slots} 本",
                f"{random_catalog['path']} / {len(shape_rounds)} shapes x {expected_slots} videos",
            ),
            "count": random_catalog["count"],
        },
    }


def resolve_configured_slots(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    return build_shape_rounds(config)


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


def sanitize_filename_component(value: str) -> str:
    sanitized_chars: list[str] = []
    previous_was_separator = False
    for char in str(value).strip():
        if char.isalnum():
            sanitized_chars.append(char)
            previous_was_separator = False
            continue
        if char in {" ", "-", "_"} and not previous_was_separator:
            sanitized_chars.append("_")
            previous_was_separator = True

    sanitized = "".join(sanitized_chars).strip("_")
    if not sanitized:
        return "user"
    return sanitized[:48]


def build_submission_filename(user_name: str) -> str:
    now = time.time()
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    milliseconds = int((now % 1) * 1000)
    user_component = sanitize_filename_component(user_name)
    return f"{timestamp}-{milliseconds:03d}_{user_component}.csv"


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


def upload_submission_csv_to_r2(
    *,
    config: dict[str, Any],
    submission_id: str,
    attachment_name: str,
    csv_text: str,
) -> dict[str, str]:
    settings = get_results_r2_archive_settings(config)
    if not settings["enabled"]:
        return {"status": "skipped", "message": bilingual("R2 退避は無効です。", "R2 archival is disabled.")}

    try:
        import boto3
    except Exception:
        return {
            "status": "failed",
            "message": bilingual(
                "R2 退避に必要な boto3 が未インストールです。",
                "boto3 is required for R2 archival but is not installed.",
            ),
        }

    bucket = os.environ.get(settings["bucketEnv"], "") or settings["bucket"]
    endpoint_url = os.environ.get(settings["endpointEnv"], "") or settings["endpointUrl"]
    access_key = os.environ.get(settings["accessKeyEnv"], "") or settings["accessKey"]
    secret_key = os.environ.get(settings["secretKeyEnv"], "") or settings["secretKey"]
    session_token = os.environ.get(settings["sessionTokenEnv"], "") or settings["sessionToken"]
    prefix = os.environ.get(settings["prefixEnv"], "") or settings["prefix"]

    missing: list[str] = []
    if not bucket:
        missing.append(settings["bucketEnv"])
    if not endpoint_url:
        missing.append(settings["endpointEnv"])
    if not access_key:
        missing.append(settings["accessKeyEnv"])
    if not secret_key:
        missing.append(settings["secretKeyEnv"])
    if missing:
        return {
            "status": "skipped",
            "message": bilingual(
                f"R2 退避設定が不足しています: {', '.join(missing)}",
                f"R2 archival settings are missing: {', '.join(missing)}",
            ),
        }

    object_key = "/".join(
        part for part in [prefix.strip("/"), f"{submission_id}_{attachment_name}"] if part
    )

    try:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token or None,
            region_name=settings["regionName"],
        )
        client.put_object(
            Bucket=bucket,
            Key=object_key,
            Body=csv_text.encode("utf-8-sig"),
            ContentType="text/csv; charset=utf-8",
            CacheControl="no-store",
            Metadata={
                "submission-id": submission_id,
                "file-name": attachment_name,
            },
        )
    except Exception as error:
        return {
            "status": "failed",
            "message": bilingual(
                f"R2 退避に失敗しました: {error}",
                f"R2 archival failed: {error}",
            ),
        }

    return {
        "status": "sent",
        "message": bilingual(
            f"R2 へ CSV を保存しました: {bucket}/{object_key}",
            f"Saved the CSV to R2: {bucket}/{object_key}",
        ),
        "objectKey": object_key,
    }


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

    resolved = build_shape_rounds(config)
    session_token = create_access_session(
        config=config,
        user_name=user_name,
        shape_rounds=resolved["shapeRounds"],
        random_source=resolved["randomSource"],
    )

    return {
        "message": readiness["message"],
        "status": readiness["status"],
        "sessionToken": session_token,
        "randomSource": resolved["randomSource"],
        "shapeRounds": resolved["shapeRounds"],
        "slots": resolved["slots"],
    }


def build_bootstrap_payload() -> dict[str, Any]:
    config = load_config()
    questions = load_questions(config)
    access_control = get_access_control_settings(config)
    shape_plan = build_shape_rounds(config)
    shape_settings = load_shape_survey_settings(config)
    apps_script_settings = get_apps_script_sync_settings(config)

    return {
        "title": config["title"],
        "subtitle": config["subtitle"],
        "slots": int(config["slots"]),
        "shapesPerQuestion": len(shape_plan["shapeRounds"]),
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
            f"1つの質問につき {len(shape_plan['shapeRounds'])} 形状を順番に評価します。各形状では {config['slots']} 本の動画を同時に比較し、すべて回答し終えたら次の質問へ進みます。",
            f"For each question, you will rate {len(shape_plan['shapeRounds'])} shapes in sequence. Each shape shows {config['slots']} videos at once, and the next question starts only after all shapes are answered.",
        ),
        "referenceSlotLabel": shape_settings["referenceSlotLabel"],
        "referenceMethodCode": shape_settings["referenceMethodCode"],
        "randomSource": shape_plan["randomSource"],
        "slotsResolved": [],
        "accessControl": {
            "enabled": bool(access_control["enabled"]),
            "sessionTtlMinutes": int(access_control.get("sessionTtlMinutes", 720)),
        },
        "googleSheetsUrl": apps_script_settings.get("sheetUrl", ""),
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
    session_rounds = {
        int(round_info["shapeIndex"]): round_info
        for round_info in (session or {}).get("shapeRounds", [])
        if isinstance(round_info, dict) and "shapeIndex" in round_info
    }
    shape_round_count = len(session_rounds)
    if session is not None and shape_round_count <= 0:
        raise AppError(
            bilingual(
                "開始セッションに形状セットがありません。もう一度開始してください。",
                "The start session does not contain shape sets. Start the survey again.",
            ),
            HTTPStatus.UNAUTHORIZED,
        )

    expected_responses = int(config["slots"]) * len(questions) * max(shape_round_count, 1)
    if len(responses) != expected_responses:
        raise AppError(
            bilingual(
                f"回答数が不正です。{len(questions)} 問 x {shape_round_count} 形状 x {config['slots']} 本の合計 {expected_responses} 件必要です。",
                f"Invalid number of responses. {expected_responses} responses are required for {len(questions)} questions x {shape_round_count} shapes x {config['slots']} videos.",
            )
        )

    validated_responses: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, int, int]] = set()
    for response in responses:
        try:
            slot_index = int(response["slotIndex"])
            question_index = int(response["questionIndex"])
            shape_index = int(response["shapeIndex"])
            rating = int(response["rating"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError(bilingual("評価は 1 から 5 の整数で指定してください。", "Ratings must be integers from 1 to 5.")) from exc
        if rating < 1 or rating > 5:
            raise AppError(bilingual("評価は 1 から 5 の範囲で指定してください。", "Ratings must be between 1 and 5."))
        if slot_index < 0 or slot_index >= int(config["slots"]):
            raise AppError(bilingual("不正な動画番号が送信されました。", "An invalid video index was submitted."))
        if question_index < 0 or question_index >= len(questions):
            raise AppError(bilingual("不正な設問番号が送信されました。", "An invalid question index was submitted."))
        if shape_index < 0 or shape_index >= shape_round_count:
            raise AppError(bilingual("不正な形状番号が送信されました。", "An invalid shape index was submitted."))

        question_id = str(response.get("questionId", "")).strip()
        if question_id not in question_map:
            raise AppError(bilingual("不正な設問IDが送信されました。", "An invalid question ID was submitted."))

        seen_key = (question_id, shape_index, slot_index)
        if seen_key in seen_pairs:
            raise AppError(bilingual("同じ設問・形状・動画の回答が重複しています。", "Duplicate responses were submitted for the same question, shape, and video."))
        seen_pairs.add(seen_key)

        session_round = session_rounds.get(shape_index)
        if session is not None and session_round is None:
            raise AppError(
                bilingual(
                    "開始セッションに存在しない形状セットが送信されました。",
                    "A shape set not present in the start session was submitted.",
                )
            )
        session_slots = {
            int(slot["slotIndex"]): slot
            for slot in (session_round or {}).get("slots", [])
            if isinstance(slot, dict) and "slotIndex" in slot
        }
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
            normalized_shape_id = str(session_round.get("shapeId", ""))
            normalized_shape_label = str(session_round.get("shapeLabel", ""))
            normalized_video = {
                "id": str(expected_video.get("id", "")),
                "title": str(expected_video.get("title", "")),
                "objectKey": str(expected_video.get("objectKey", "")).strip(),
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
            normalized_shape_id = str(response.get("shapeId", ""))
            normalized_shape_label = str(response.get("shapeLabel", ""))
            normalized_video = {
                "id": str(video.get("id", "")),
                "title": str(video["title"]),
                "objectKey": str(video.get("objectKey", "")).strip(),
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
                "shapeIndex": shape_index,
                "shapeId": normalized_shape_id,
                "shapeLabel": normalized_shape_label,
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

    validated_responses.sort(key=lambda item: (item["questionIndex"], item["shapeIndex"], item["slotIndex"]))
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
    download_filename = build_submission_filename(validated["userName"])

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

    results_r2_result = upload_submission_csv_to_r2(
        config=config,
        submission_id=submission_id,
        attachment_name=download_filename,
        csv_text=csv_text,
    )
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
        "resultsR2Status": results_r2_result["status"],
        "resultsR2Message": results_r2_result["message"],
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
                        "shapeRounds": result["shapeRounds"],
                        "slots": result["slots"],
                    }
                )
                return
            if parsed.path == "/api/resolve-slots":
                config = load_config()
                session = require_access_session(payload, config)
                resolved = build_shape_rounds(config)
                if session is not None:
                    update_access_session_slots(
                        str(payload.get("sessionToken", "")).strip(),
                        shape_rounds=resolved["shapeRounds"],
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
                        "resultsR2Status": result["resultsR2Status"],
                        "resultsR2Message": result["resultsR2Message"],
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
