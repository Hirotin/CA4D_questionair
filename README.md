# Video Survey Studio

`Video Survey Studio` is a lightweight Python survey app for side-by-side video evaluation.

## Current Survey Design

- `5` questions
- `7` shapes per question
- `20` videos per shape
- video order is shuffled within each shape
- before each question starts, the app shows a question-only interstitial page with `回答を始める「Begin Rating」`
- while that interstitial is shown, the next video set is preloaded in the background
- the `similarity_to_video_0` question shows `method 0011 (input gt)` as the fixed reference panel labeled `動画0「Video 0」`
- after all `7` shapes are rated for one question, the app advances to the next question
- videos in the same round are reset and started together, with periodic drift correction during playback

The current dataset is driven by:

- [app_config.json](/Users/hiroki/.ssh/video-survey-app/data/app_config.json)
- [video_catalog_20260508_all_latest_r2.csv](/Users/hiroki/.ssh/video-survey-app/data/video_catalog_20260508_all_latest_r2.csv)

## Run Locally

```bash
cd /Users/hiroki/.ssh/video-survey-app
export SURVEY_START_PASSWORD='your-start-password'
python3 server.py --port 8000
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Data Flow

The app loads the catalog CSV into SQLite on startup, groups videos by `sample_name`, and builds one shuffled `20-video` round per shape.

Current catalog semantics:

- `sample_name`: shape ID such as `1013`
- `method_name`: method code such as `0001`
- `video_code`: shape-method identifier such as `1013-0001`
- `prompt_text`: method label from the ZIP mapping CSV, such as `4dgen fp`

The generated link list is exported to:

- [video_links.csv](/Users/hiroki/.ssh/video-survey-app/data/video_links.csv)

## Output CSV

Each submission creates:

- one aggregate CSV append in [survey_results.csv](/Users/hiroki/.ssh/video-survey-app/responses/survey_results.csv)
- one per-submission CSV in [responses/submissions](/Users/hiroki/.ssh/video-survey-app/responses/submissions)
- one browser download named `YYYYMMDD-HHMMSS-ms_UserName.csv`

The saved columns are:

- `user_name`
- `video_code`
- `question_text`
- `score`

## Google Sheets Sync

If configured, the server also posts the same rows to an Apps Script endpoint.

Relevant config:

- `SURVEY_APPS_SCRIPT_ENDPOINT`
- `SURVEY_APPS_SCRIPT_TOKEN`

The start button runs a write-availability check before the survey opens.

## Access Control

Set the survey start password with:

```bash
export SURVEY_START_PASSWORD='your-start-password'
```

Users must enter:

- `User名「User Name」`
- `開始パスワード「Start Password」`

before the videos are shown.

## R2 Video Storage

The app resolves `object_key` values with:

- `videoStorage.publicBaseUrl` in [app_config.json](/Users/hiroki/.ssh/video-survey-app/data/app_config.json), or
- `SURVEY_VIDEO_PUBLIC_BASE_URL`

Current public base URL:

- [R2 public URL](https://pub-d59f91e9afbd4a4fb7ceea0a9d7c09bb.r2.dev)

## Upload a New ZIP to R2

Create a virtual environment once:

```bash
cd /Users/hiroki/.ssh/video-survey-app
python3 -m venv .venv
.venv/bin/pip install boto3
```

Then upload:

```bash
AWS_ACCESS_KEY_ID='...'
AWS_SECRET_ACCESS_KEY='...'
R2_BUCKET_NAME='ca4d-questionair'
R2_ENDPOINT_URL='https://<account-id>.r2.cloudflarestorage.com'
.venv/bin/python scripts/upload_zip_to_r2.py /path/to/your.zip --output-csv data/video_catalog_20260508_all_latest_r2.csv
```

The current upload script reads the ZIP mapping CSV and uploads only the mapped `140` MP4 files.

If a mapped video is not already `16` frames, the upload script rebuilds it to `16` frames by:

- keeping the first frame
- keeping the last frame
- sampling `14` frames from the interior range

## Deploy on Render

The repo includes [render.yaml](/Users/hiroki/.ssh/video-survey-app/render.yaml).

Set these environment variables in Render as needed:

- `SURVEY_START_PASSWORD`
- `SURVEY_VIDEO_PUBLIC_BASE_URL`
- `SURVEY_APPS_SCRIPT_ENDPOINT`
- `SURVEY_APPS_SCRIPT_TOKEN`
- `SURVEY_SMTP_PASSWORD`

## Verification

Basic checks used during development:

```bash
python3 -m py_compile server.py
node --check static/app.js
python3 -m json.tool data/app_config.json > /dev/null
```
