#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server import (
    BASE_DIR,
    DATABASE_SOURCE_LABEL,
    ensure_random_video_database,
    get_database_path,
    get_video_storage_settings,
    load_config,
    load_csv_rows,
    normalize_catalog_csv_row,
)


def import_catalog(csv_path: Path) -> dict[str, int]:
    config = load_config()
    ensure_random_video_database(config)
    database_path = get_database_path(config)
    storage_settings = get_video_storage_settings(config)
    rows = load_csv_rows(csv_path)

    imported_count = 0
    with sqlite3.connect(database_path) as connection:
        for raw_row in rows:
            normalized = normalize_catalog_csv_row(
                raw_row,
                public_base_url=storage_settings["publicBaseUrl"],
                default_source_label=DATABASE_SOURCE_LABEL,
            )
            is_active = 0 if raw_row.get("is_active", "").strip() in {"0", "false", "False"} else 1
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
                    is_active = excluded.is_active,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    normalized["id"],
                    normalized["title"],
                    normalized["description"],
                    normalized["objectKey"],
                    normalized["url"],
                    normalized["sourceLabel"],
                    normalized["videoGroup"],
                    normalized["videoCode"],
                    normalized["methodName"],
                    normalized["sampleName"],
                    normalized["promptText"],
                    is_active,
                ),
            )
            imported_count += 1

    return {"rows": len(rows), "imported": imported_count}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import video metadata CSV into the random video catalog database."
    )
    parser.add_argument("csv_path", help="Path to a CSV file with video metadata.")
    args = parser.parse_args()

    csv_path = (Path(args.csv_path).expanduser())
    if not csv_path.is_absolute():
        csv_path = (BASE_DIR / csv_path).resolve()

    result = import_catalog(csv_path)
    print(
        f"Imported {result['imported']} rows from {csv_path} into {get_database_path(load_config())}."
    )


if __name__ == "__main__":
    main()
