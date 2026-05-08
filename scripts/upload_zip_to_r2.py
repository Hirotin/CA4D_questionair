#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import zipfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


MAPPING_SUFFIX = "_mapping.csv"
VIDEO_GROUP = "R2-20260508-ALL-LATEST"


def load_mapping_rows(zf: zipfile.ZipFile) -> list[dict[str, str]]:
    mapping_name = ""
    for name in zf.namelist():
        if name.lower().endswith(MAPPING_SUFFIX):
            mapping_name = name
            break

    if not mapping_name:
        raise RuntimeError("Could not find the mapping CSV inside the ZIP archive.")

    with zf.open(mapping_name) as handle:
        reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8-sig"))
        return [{str(key): str(value or "") for key, value in row.items()} for row in reader]


def build_catalog_row(zip_prefix: str, row: dict[str, str]) -> dict[str, str]:
    shape_id = str(row.get("shape_id", "")).strip()
    sequence = str(row.get("sequence", "")).strip()
    method_label = str(row.get("method_label", "")).strip()
    file_name = str(row.get("file_name", "")).strip()
    original_name = str(row.get("original_name", "")).strip()
    object_key = f"{zip_prefix}/{file_name}"

    title_parts = [f"Shape {shape_id}", f"Method {sequence}"]
    if method_label:
        title_parts.append(method_label)

    description = f"Shape {shape_id}, method {sequence}"
    if method_label:
        description += f" ({method_label})"
    if original_name:
        description += f", source file {original_name}"
    description += "."

    return {
        "id": f"r2-{shape_id}-{sequence}",
        "title": " / ".join(title_parts),
        "description": description,
        "object_key": object_key,
        "video_url": "",
        "source_label": "データベースランダム「Database Random」",
        "video_group": VIDEO_GROUP,
        "video_code": f"{shape_id}-{sequence}",
        "method_name": sequence,
        "sample_name": shape_id,
        "prompt_text": method_label,
        "is_active": "1",
    }


def upload_zip(
    *,
    zip_path: Path,
    bucket_name: str,
    endpoint_url: str,
    output_csv_path: Path,
) -> dict[str, int]:
    try:
        import boto3
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "boto3 is required. Install it in a virtual environment before running this script."
        ) from exc

    access_key_id = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
    secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    if not access_key_id or not secret_access_key:
        raise RuntimeError("Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY before running this script.")

    s3 = boto3.client(
        service_name="s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
    )

    uploaded_count = 0
    catalog_rows: list[dict[str, str]] = []
    with zipfile.ZipFile(zip_path) as zf:
        rows = load_mapping_rows(zf)
        zip_prefix = Path(rows[0]["file_name"]).parent.as_posix() if "/" in rows[0]["file_name"] else Path(zf.namelist()[0]).parts[0]
        root_prefix = Path(zf.namelist()[0]).parts[0]

        for row in rows:
            file_name = str(row.get("file_name", "")).strip()
            if not file_name:
                continue
            member_name = f"{root_prefix}/{file_name}"
            object_key = member_name
            with zf.open(member_name) as source:
                s3.upload_fileobj(
                    source,
                    bucket_name,
                    object_key,
                    ExtraArgs={"ContentType": "video/mp4"},
                )
            catalog_rows.append(build_catalog_row(root_prefix, row))
            uploaded_count += 1

    output_csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "id",
        "title",
        "description",
        "object_key",
        "video_url",
        "source_label",
        "video_group",
        "video_code",
        "method_name",
        "sample_name",
        "prompt_text",
        "is_active",
    ]
    with output_csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(catalog_rows)

    return {"uploaded": uploaded_count, "catalogRows": len(catalog_rows)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload mapped MP4 files in a ZIP archive to Cloudflare R2 and generate a catalog CSV."
    )
    parser.add_argument("zip_path", help="Path to the ZIP archive containing MP4 files.")
    parser.add_argument(
        "--bucket",
        default=os.environ.get("R2_BUCKET_NAME", ""),
        help="R2 bucket name. Defaults to R2_BUCKET_NAME.",
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("R2_ENDPOINT_URL", ""),
        help="R2 S3 endpoint URL. Defaults to R2_ENDPOINT_URL.",
    )
    parser.add_argument(
        "--output-csv",
        default=str(PROJECT_DIR / "data" / "video_catalog_20260508_all_latest_r2.csv"),
        help="Path to the generated catalog CSV.",
    )
    args = parser.parse_args()

    zip_path = Path(args.zip_path).expanduser().resolve()
    output_csv_path = Path(args.output_csv).expanduser().resolve()
    if not args.bucket:
        raise SystemExit("Bucket name is required. Set --bucket or R2_BUCKET_NAME.")
    if not args.endpoint:
        raise SystemExit("Endpoint URL is required. Set --endpoint or R2_ENDPOINT_URL.")

    result = upload_zip(
        zip_path=zip_path,
        bucket_name=args.bucket,
        endpoint_url=args.endpoint,
        output_csv_path=output_csv_path,
    )
    print(
        f"Uploaded {result['uploaded']} videos to {args.bucket} and wrote {result['catalogRows']} rows to {output_csv_path}."
    )


if __name__ == "__main__":
    main()
