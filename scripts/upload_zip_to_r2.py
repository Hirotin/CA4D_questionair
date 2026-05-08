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

def sanitize_method_name(source_name: str) -> str:
    candidate = str(source_name).strip()
    if candidate.lower().endswith(".mp4"):
        candidate = candidate[:-4]
    return candidate or "Method"


def build_method_overrides(zf: zipfile.ZipFile) -> dict[tuple[str, str], str]:
    overrides: dict[tuple[str, str], str] = {}
    mapping_name = None
    for name in zf.namelist():
        if name.endswith("20260508_updated_method_renders_mapping.csv"):
            mapping_name = name
            break
    if not mapping_name:
        return overrides

    with zf.open(mapping_name) as handle:
        reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8-sig"))
        for row in reader:
            shape_id = str(row.get("shape_id", "")).strip()
            new_name = Path(str(row.get("new_name", "")).strip()).stem
            source_name = sanitize_method_name(str(row.get("source_name", "")).strip())
            if not shape_id or not new_name:
                continue
            method_index = new_name.split("_")[-1]
            overrides[(shape_id, method_index)] = source_name
    return overrides


def build_catalog_row(
    zip_member_name: str, *, object_key: str, method_overrides: dict[tuple[str, str], str]
) -> dict[str, str]:
    stem = Path(zip_member_name).stem
    _, shape_id, method_index = stem.split("_")
    method_name = method_overrides.get((shape_id, method_index), f"Method {method_index}")
    return {
        "id": f"r2-{shape_id}-{method_index}",
        "title": f"R2 Sample / {shape_id} / {method_index}",
        "description": f"R2 uploaded sample for shape {shape_id}, method {method_index}.",
        "object_key": object_key,
        "video_url": "",
        "source_label": "データベースランダム「Database Random」",
        "video_group": "R2-20260508",
        "video_code": f"R2-{shape_id}-{method_index}",
        "method_name": method_name,
        "sample_name": f"Shape {shape_id}",
        "prompt_text": "",
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
        method_overrides = build_method_overrides(zf)
        mp4_names = sorted(
            name
            for name in zf.namelist()
            if name.lower().endswith(".mp4") and not name.endswith("/")
        )
        for member_name in mp4_names:
            object_key = member_name
            with zf.open(member_name) as source:
                s3.upload_fileobj(source, bucket_name, object_key, ExtraArgs={"ContentType": "video/mp4"})
            catalog_rows.append(
                build_catalog_row(
                    member_name,
                    object_key=object_key,
                    method_overrides=method_overrides,
                )
            )
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
        description="Upload all MP4 files in a ZIP archive to Cloudflare R2 and generate a catalog CSV."
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
        default=str(PROJECT_DIR / "data" / "video_catalog_20260508_r2.csv"),
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
