# 動画アンケートシステム

6つの動画を横並びで表示するアンケートアプリです。質問は動画の上に表示され、各動画の下に 1〜5 の評価ボタンが付きます。

- 動画1は固定表示
- 動画2〜6は SQLite データベースからランダム抽選
- ローカル MP4 と YouTube 埋め込みの両方に対応
- 中央に1つの設問を大きく表示
- 開始画面で `User名` と開始パスワードを入力
- 各設問に対して6本すべてへ 1〜5 の数字で回答
- `次の質問` で次の設問へ進み、最終設問で送信
- 未回答があると警告して先へ進めない
- 送信後は完了画面のみを表示
- 動画一覧は `data/video_links.csv` に出力
- 集計結果は `responses/survey_results.csv` に `User名` と動画識別番号付きで追記
- 各回答のCSVは回答端末へ自動ダウンロード
- SMTP を設定すると各回答CSVをメール添付で自動送信
- Apps Script Webアプリを設定すると Google Sheets にも自動追記
- `回答を始める` の直前に Google Sheets への保存可否を自動診断し、通るまで開始しない
- `回答を始める` の前に開始パスワードで認証できる
- `Cloudflare R2` の公開URLを `object_key + base URL` で扱える

## 起動方法

```bash
cd /Users/hiroki/.ssh/video-survey-app
python3 server.py --port 8000
```

ブラウザで [http://127.0.0.1:8000](http://127.0.0.1:8000) を開いてください。

公開環境では `PORT` と `HOST` の環境変数があれば自動でそれを使います。たとえば `PORT=10000` なら `0.0.0.0:10000` で待ち受けます。

## Render で公開する最小手順

1. この [video-survey-app](/Users/hiroki/.ssh/video-survey-app) を GitHub に push する
2. Render で `New > Web Service` を作る
3. リポジトリを選ぶ
4. `Start Command` に `python3 server.py` を入れる
5. `Environment Variables` に少なくとも次を入れる

- `SURVEY_START_PASSWORD`
- `SURVEY_APPS_SCRIPT_ENDPOINT` が必要なら設定
- `SURVEY_APPS_SCRIPT_TOKEN` が必要なら設定
- `SURVEY_VIDEO_PUBLIC_BASE_URL` を R2 で使うなら設定

Render の Web Service は公開URLを持ち、アプリは `0.0.0.0` で待ち受ける必要があります。今の `server.py` は `PORT` があれば自動で公開向けに待ち受けます。

このリポジトリには [render.yaml](/Users/hiroki/.ssh/video-survey-app/render.yaml) を入れてあります。Render で `Blueprint` として読み込めば、`SURVEY_START_PASSWORD` などの `sync: false` 変数は初回作成時に入力を求められます。[Blueprint YAML Reference](https://render.com/docs/blueprint-spec)

注意:
- Render の free web service はファイルシステムが ephemeral です。`responses/` に書いた CSV は再デプロイや再起動で消えます。[Persistent Disks](https://render.com/docs/disks) [Deploy for Free](https://render.com/docs/free)
- 長期保存したい場合は Google Sheets 連携を有効にするか、有料プランで persistent disk を付けてください。[Persistent Disks](https://render.com/docs/disks)

## R2 で動画を置く手順

大量動画を扱う場合は、YouTube ではなく `Cloudflare R2` の公開バケットを前提にするのが運用しやすいです。

1. `R2` に動画をまとめてアップロードする
2. 公開URLのベースを決める
3. このアプリに `SURVEY_VIDEO_PUBLIC_BASE_URL` を設定する
4. 動画メタデータCSVに `object_key` を並べて一括投入する

`SURVEY_VIDEO_PUBLIC_BASE_URL` には、たとえば次のような公開URLのベースを入れます。

```bash
export SURVEY_VIDEO_PUBLIC_BASE_URL='https://pub-xxxxxxxxxxxxxxxx.r2.dev'
```

`app_config.json` に直接書くなら [app_config.json](/Users/hiroki/.ssh/video-survey-app/data/app_config.json) の `videoStorage.publicBaseUrl` です。本番では環境変数 `SURVEY_VIDEO_PUBLIC_BASE_URL` が優先されます。

```json
{
  "videoStorage": {
    "provider": "r2",
    "publicBaseUrl": "",
    "publicBaseUrlEnv": "SURVEY_VIDEO_PUBLIC_BASE_URL"
  }
}
```

`object_key` を使うと、CSV や DB では `folder/sample.mp4` のようなキーだけ管理し、実際の再生URLはアプリ側で組み立てます。

## データベース

ランダム動画は `data/random_video_catalog.sqlite3` を使います。サーバー起動時または初回アクセス時に自動作成され、[app_config.json](/Users/hiroki/.ssh/video-survey-app/data/app_config.json) の `randomVideoDatabase.seedVideos` を初期登録します。

テーブル名は `videos` です。`is_active = 1` の動画だけが抽選対象です。

主なカラム:

- `id`
- `title`
- `description`
- `object_key`
- `url`
- `source_label`
- `video_group`
- `video_code`
- `method_name`
- `sample_name`
- `prompt_text`
- `is_active`

`url` / `objectKey` には次のような入れ方ができます。

- ローカル動画: `/static/videos/example.mp4`
- YouTube 動画: `https://www.youtube.com/watch?v=VIDEO_ID` または `https://youtu.be/VIDEO_ID`
- R2 動画: `objectKey: "folder/example.mp4"` と `SURVEY_VIDEO_PUBLIC_BASE_URL`
- R2 動画: `url: "folder/example.mp4"` と `SURVEY_VIDEO_PUBLIC_BASE_URL`

YouTube URL が入っている場合は、フロント側で YouTube IFrame API を使った埋め込み再生に自動で切り替わります。6本ともミュート状態で同時再生し、無限ループします。

現在のサンプル動画はすべて `videoGroup: "DEMO"` を持ち、CSV には `DEMO-001` のような `videoCode` が保存されます。`methodName`, `sampleName`, `promptText` もここで管理します。

## R2 / YouTube 設定例

固定動画もランダム動画も、設定上は `url` か `objectKey` を使います。YouTube にしたい場合は `url` を共有URLへ置き換えるだけです。R2 にしたい場合は `objectKey` を使うのが楽です。

```json
{
  "id": "fixed-r2-demo",
  "title": "Reference Video",
  "description": "固定表示する動画です。",
  "objectKey": "fixed/reference-video.mp4",
  "sourceLabel": "固定動画",
  "videoGroup": "R2",
  "videoCode": "R2-001",
  "methodName": "Method A",
  "sampleName": "Reference 01",
  "promptText": "Prompt text for the fixed reference video."
}
```

または:

```json
{
  "id": "fixed-demo",
  "title": "Reference Video",
  "description": "固定表示する動画です。",
  "url": "https://www.youtube.com/watch?v=M7lc1UVf-VE",
  "sourceLabel": "固定動画",
  "videoGroup": "DEMO",
  "videoCode": "DEMO-001",
  "methodName": "YouTube Demo",
  "sampleName": "Fixed Sample 01",
  "promptText": "Demo prompt for fixed reference video."
}
```

ランダム枠は [app_config.json](/Users/hiroki/.ssh/video-survey-app/data/app_config.json) の `randomVideoDatabase.seedVideos` を編集すれば、同じ `id` のレコードが次回起動時に自動更新されます。

## CSV 一括投入

大量動画を登録する場合は、CSV を作って DB に流し込むのが一番楽です。追加したスクリプトは [scripts/import_video_catalog_csv.py](/Users/hiroki/.ssh/video-survey-app/scripts/import_video_catalog_csv.py) です。

CSV の主な列:

- `id`
- `title`
- `description`
- `object_key`
- `video_url`
- `source_label`
- `video_group`
- `video_code`
- `method_name`
- `sample_name`
- `prompt_text`
- `is_active`

最低限は `video_code` と、`object_key` か `video_url` のどちらかがあれば流し込めます。

ひな形は [data/video_catalog_template.csv](/Users/hiroki/.ssh/video-survey-app/data/video_catalog_template.csv) に置いてあります。

実行例:

```bash
cd /Users/hiroki/.ssh/video-survey-app
export SURVEY_VIDEO_PUBLIC_BASE_URL='https://pub-xxxxxxxxxxxxxxxx.r2.dev'
python3 scripts/import_video_catalog_csv.py /path/to/video_catalog.csv
```

`object_key` が入っている行は、`SURVEY_VIDEO_PUBLIC_BASE_URL` を使って再生URLへ変換されます。

## 設問設定

設問は [app_config.json](/Users/hiroki/.ssh/video-survey-app/data/app_config.json) の `questions` 配列で管理します。

```json
{
  "id": "overall",
  "text": "この動画の総合的な魅力を評価してください。"
}
```

## メール送信設定

[app_config.json](/Users/hiroki/.ssh/video-survey-app/data/app_config.json) の `mailDelivery` で SMTP を設定すると、各回答の CSV がメール添付で自動送信されます。

```json
{
  "mailDelivery": {
    "enabled": true,
    "smtpHost": "smtp.example.com",
    "smtpPort": 587,
    "useStartTls": true,
    "useSsl": false,
    "username": "mailer@example.com",
    "passwordEnv": "SURVEY_SMTP_PASSWORD",
    "fromAddress": "mailer@example.com",
    "toAddress": "collector@example.com",
    "subjectPrefix": "動画アンケート結果"
  }
}
```

SMTP パスワードは JSON に直接書かず、環境変数 `SURVEY_SMTP_PASSWORD` に入れてください。

## 開始パスワード設定

[app_config.json](/Users/hiroki/.ssh/video-survey-app/data/app_config.json) の `accessControl` で、回答開始前のパスワード認証を設定できます。

```json
{
  "accessControl": {
    "enabled": true,
    "password": "change-me",
    "passwordEnv": "SURVEY_START_PASSWORD",
    "sessionTtlMinutes": 720
  }
}
```

- `enabled`: `true` なら開始前にパスワードを要求
- `password`: ローカルや簡易検証用の固定パスワード
- `passwordEnv`: 本番ではこちらの環境変数を優先
- `sessionTtlMinutes`: 認証後セッションの有効時間

公開時は `SURVEY_START_PASSWORD` をホスティング側の環境変数に入れてください。公開リポジトリの `app_config.json` には開始パスワードを保持しない想定です。

## Apps Script 連携

Apps Script 側の雛形は [google_apps_script_web_app.gs](/Users/hiroki/.ssh/video-survey-app/google_apps_script_web_app.gs) に置いてあります。Google Sheets で `拡張機能 > Apps Script` を開き、この内容を貼って Web アプリとしてデプロイしてください。

シートの1行目は次の列にしておくと扱いやすいです。

- `timestamp`
- `user_name`
- `video_code`
- `question_text`
- `score`
- `submission_id`

Apps Script 側の `Script Properties` には次を入れます。

- `API_TOKEN`
- `SHEET_ID`
- `SHEET_NAME`

アプリ側は [app_config.json](/Users/hiroki/.ssh/video-survey-app/data/app_config.json) の `appsScriptSync` を設定します。

```json
{
  "appsScriptSync": {
    "enabled": true,
    "endpointUrl": "",
    "endpointEnv": "SURVEY_APPS_SCRIPT_ENDPOINT",
    "token": "",
    "tokenEnv": "SURVEY_APPS_SCRIPT_TOKEN",
    "timeoutSeconds": 15
  }
}
```

`endpointUrl` と `token` を `app_config.json` に直接書いても動きますが、公開リポジトリでは空にして、`SURVEY_APPS_SCRIPT_ENDPOINT` と `SURVEY_APPS_SCRIPT_TOKEN` を環境変数で入れる運用を推奨します。Apps Script が有効なら、ローカル保存・端末ダウンロード・メール送信に加えて Google Sheets にも同じ回答行が追記されます。

開始時に `回答を始める` を押すと、アプリはまず Apps Script へ空の `rows` を送って Google Sheets 側の到達性と設定整合性を診断します。ここで失敗した場合は動画を開かず、開始前バナーとトーストに失敗理由を表示します。

## 動画リンク CSV

動画メタデータは [data/video_links.csv](/Users/hiroki/.ssh/video-survey-app/data/video_links.csv) に出力されます。固定動画とランダム候補の両方がここにまとまります。動画セットを解決したタイミングで上書き更新されます。

- `video_code`
- `object_key`
- `video_url`
- `method_name`
- `sample_name`
- `prompt_text`

## 集計 CSV

回答は `responses/survey_results.csv` に追記されます。1回の送信につき `設問数 × 6` 行追加され、各行に以下が入ります。

- `user_name`
- `video_code`
- `question_text`
- `score`

集計CSVは匿名寄りの最小構成にしてあり、詳細なリンクやメタ情報は `data/video_links.csv` 側で対応付けます。

## 端末保存

回答を送信すると、その送信分だけの CSV が `YYYYMMDD-HHMMSS-ms_User名.csv` という形式でブラウザから各端末へ自動ダウンロードされます。同じ CSV が、SMTP 設定済みならメール添付としても送信されます。
