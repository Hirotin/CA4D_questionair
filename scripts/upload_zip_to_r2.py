#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


MAPPING_SUFFIX = "_mapping.csv"
VIDEO_GROUP = "R2-20260508-ALL-LATEST"
TARGET_FRAME_COUNT = 16
SHAPE_PROMPTS = {
    "1013": "A dinosaur lowering its head",
    "1047": "A character raising both hands / A character throwing their hands up in the air",
    "1143": "A dinosaur shakes its head from side to side, then raises it and roars",
    "1189": "A bear rearing up on its hind legs",
    "1230": "A character moving both arms backward / A character putting their hands behind their back",
    "1232": "A character raising a sword high / A character holding a sword aloft",
    "1445": "A character spreads their long limbs, appearing larger",
}


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
    prompt_text = SHAPE_PROMPTS.get(shape_id, "")

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
        "prompt_text": prompt_text,
        "is_active": "1",
    }


def probe_frame_count(video_path: Path) -> int:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_read_frames",
        "-of",
        "json",
        str(video_path),
    ]
    payload = json.loads(subprocess.check_output(command))
    streams = payload.get("streams", [])
    if not streams:
        return 0
    return int(streams[0].get("nb_read_frames") or 0)


def build_sample_indices(frame_count: int) -> list[int]:
    if frame_count <= 0:
        return []
    if frame_count == TARGET_FRAME_COUNT:
        return list(range(frame_count))
    if frame_count < TARGET_FRAME_COUNT:
        raise RuntimeError(
            f"Cannot normalize a video with only {frame_count} frames to {TARGET_FRAME_COUNT} frames."
        )

    interior_target = TARGET_FRAME_COUNT - 2
    interior_start = 1
    interior_end = frame_count - 2
    positions = []
    for index in range(interior_target):
        if interior_target == 1:
            candidate = interior_start
        else:
            ratio = index / (interior_target - 1)
            candidate = round(interior_start + ((interior_end - interior_start) * ratio))
        positions.append(int(candidate))

    deduped: list[int] = []
    for candidate in positions:
        if candidate not in deduped:
            deduped.append(candidate)

    cursor = interior_start
    while len(deduped) < interior_target and cursor <= interior_end:
        if cursor not in deduped:
            deduped.append(cursor)
        cursor += 1

    deduped = sorted(deduped)[:interior_target]
    return [0, *deduped, frame_count - 1]


def normalize_video_to_16_frames(source_path: Path, target_path: Path) -> int:
    frame_count = probe_frame_count(source_path)
    if frame_count == TARGET_FRAME_COUNT:
        shutil.copy2(source_path, target_path)
        return frame_count

    sample_indices = build_sample_indices(frame_count)
    if len(sample_indices) != TARGET_FRAME_COUNT:
        raise RuntimeError(
            f"Expected {TARGET_FRAME_COUNT} sample indices, got {len(sample_indices)} for {source_path.name}."
        )

    selector = "+".join(f"eq(n\\,{index})" for index in sample_indices)
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-vf",
        f"select='{selector}',setpts=N/FRAME_RATE/TB",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(target_path),
    ]
    subprocess.check_call(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    normalized_count = probe_frame_count(target_path)
    if normalized_count != TARGET_FRAME_COUNT:
        raise RuntimeError(
            f"Normalization failed for {source_path.name}: expected {TARGET_FRAME_COUNT} frames, got {normalized_count}."
        )
    return frame_count


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
    normalized_count = 0
    catalog_rows: list[dict[str, str]] = []
    with zipfile.ZipFile(zip_path) as zf:
        rows = load_mapping_rows(zf)
        root_prefix = Path(zf.namelist()[0]).parts[0]

        with tempfile.TemporaryDirectory(prefix="ca4d-r2-upload-") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            for row in rows:
                file_name = str(row.get("file_name", "")).strip()
                if not file_name:
                    continue
                member_name = f"{root_prefix}/{file_name}"
                object_key = member_name
                source_path = temp_dir / file_name
                normalized_path = temp_dir / f"normalized-{file_name}"
                source_path.parent.mkdir(parents=True, exist_ok=True)

                with zf.open(member_name) as source, source_path.open("wb") as target:
                    shutil.copyfileobj(source, target)

                original_frame_count = normalize_video_to_16_frames(source_path, normalized_path)
                if original_frame_count != TARGET_FRAME_COUNT:
                    normalized_count += 1

                with normalized_path.open("rb") as source_handle:
                    s3.upload_fileobj(
                        source_handle,
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

    return {
        "uploaded": uploaded_count,
        "catalogRows": len(catalog_rows),
        "normalized": normalized_count,
    }


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
        f"Uploaded {result['uploaded']} videos to {args.bucket}, normalized {result['normalized']} videos to {TARGET_FRAME_COUNT} frames, and wrote {result['catalogRows']} rows to {output_csv_path}."
    )


if __name__ == "__main__":
    main()
