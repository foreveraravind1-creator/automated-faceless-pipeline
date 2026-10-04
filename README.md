# Automated Faceless Instagram Reels Pipeline

A topic-in engine for faceless Instagram Reels. A niche is a JSON profile
passed in with the topic. The default command writes an unapproved script
and stops. `--dry-run` renders only after a person sets `approved` to true.
`--publish` posts only when that person passes `--confirm-publish` as well.
Nothing is queued, scheduled, or posted unattended.

```
Topic + niche JSON --► one Gemini Flash call --► script.json (approved: false)
approved script.json --► Kokoro-82M + Pexels + ffmpeg --► final_reel.mp4
approved script + final_reel.mp4 --► --publish (preview) --► --confirm-publish
```

| Stage | Role | When it runs |
|-------|------|----------------|
| Script | One Gemini Flash call | `python main.py --topic ... --niche ...` |
| Voice + picture | Local Kokoro-82M and Pexels | `--dry-run`, and only if `approved` is true |
| Render | ffmpeg, 1080x1920 H.264/AAC | same `--dry-run` |
| Publish | Plain Python, Meta Graph API | `--publish`, and only with `--confirm-publish` after approval |

---

## Quick Start (Local)

### 1. Prerequisites

- Python 3.12. Kokoro-82M does not run on Python 3.13 or newer, so the
  Dockerfile stays on `python:3.12-slim`.
- A Gemini API key (`GEMINI_API_KEY`) for the script command
- For `--dry-run`: `ffmpeg`, `espeak-ng`, a DejaVu font (`fonts-dejavu-core`),
  and `PEXELS_API_KEY`. The first Kokoro run downloads weights from Hugging Face.

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure secrets

```bash
cp .env.example .env
# Edit .env with your real credentials
```

### 4. Run the script stage

```bash
# Writes output/<job_id>/script.json with "approved": false, then exits.
python main.py --topic "Why is the sky blue?" --niche niches/example.json

# With an explicit job id (must match ^[a-z0-9-]{1,32}$)
python main.py --topic "The science of sleep" --niche niches/example.json --job-id sleep-001
```

Only `GEMINI_API_KEY` is required for that command. It does not publish.

### 4b. Render an approved script

Edit `output/<job_id>/script.json` and set `"approved": true` after review.
Then:

```bash
python main.py --dry-run --job-id sleep-001
# same thing:
python main.py --dry-run sleep-001
```

If `approved` is false, the command refuses and does not download or encode.
The reel is `output/<job_id>/final_reel.mp4` (or `$OUTPUT_DIR/<job_id>/`).
Kokoro writes `audio.wav` and `timings.json`. ffmpeg burns `on_screen_hook`
for the first 3 seconds and narration captions from those timings.
`h264_nvenc` is used only if a one-frame test encode succeeds; otherwise
`libx264`. Nothing is uploaded.

### 4c. Publish, supervised

Publishing is a separate command. It does not run as part of the script
command, `--dry-run`, or the webhook.

```bash
# Preview only. Prints the caption and the video path. No network call.
python main.py --publish --job-id sleep-001

# The only Meta call. Refuses unless approved is true and final_reel.mp4 exists.
python main.py --publish --job-id sleep-001 --confirm-publish
```

`--confirm-publish` reads `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_ACCOUNT_ID`
from the environment. It checks the live content-publishing quota, uploads
the local mp4 with a resumable upload (the file is not given a public URL),
waits until the container is `FINISHED`, then publishes once. The result is
`output/<job_id>/publish.json`. If that file already has a `post_id`, the
command refuses and does not post again.

The caption sent to Meta is `script.caption`, then a blank line, then
`script.hashtags` that are not already in the body. The script stage stores
the caption body without hashtags. `share_to_feed` is true, and
`is_ai_generated` is true.

### 5. Start the webhook server

```bash
uvicorn webhook.server:app --reload --port 8080
```

Trigger via webhook:

```bash
# Both routes only generate the script. Neither renders nor publishes.
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"topic": "Why do we dream?", "niche": "niches/example.json"}'
```

---

## Cloud Run Deployment

### Critical sizing requirements

> **WARNING: Do not deploy with Cloud Run defaults.**
>
> - **Memory**: ffmpeg transcoding a 1080x1920 clip peaks at ~1.5–2 GiB RAM.
>   Default 512 MiB / 1 GiB will SIGKILL the container (exit code 137).
>   **Minimum: `--memory 4Gi`**
>
> - **Timeout**: `/run` and `/run-async` only write a script. They do not
>   render, poll Meta, or publish, so they do not need a long request timeout
>   for a publish loop.
>
> There is no Cloud Scheduler job, Cloud Tasks queue, or Cloud Run job for
> posting. Publishing is the supervised `--publish --confirm-publish` command
> only. Do not point a scheduler at this service to post Reels.
>
> Both `/run` and `/run-async` only write an unapproved script.
> They do not render, and they do not publish.

### 1. Build and push the container

```bash
PROJECT_ID=your-gcp-project-id
IMAGE=gcr.io/$PROJECT_ID/faceless-reels-pipeline:latest

gcloud builds submit --tag $IMAGE
```

### 2. Create Secret Manager secrets

```bash
for SECRET in GEMINI_API_KEY PEXELS_API_KEY; do
  gcloud secrets create $SECRET --replication-policy="automatic"
done

# Populate each secret with your own values. Never commit them.
echo -n "your-gemini-api-key" | gcloud secrets versions add GEMINI_API_KEY --data-file=-
# Repeat for PEXELS_API_KEY.
```

The Instagram token is not a webhook secret. Put it in the environment of
the shell where you run `--confirm-publish`.

### 3. Deploy to Cloud Run

The webhook does not publish and does not need an Instagram token or a GCS
bucket. Run `--publish` where you can watch it, with the token in that
environment, not on a scheduled service.

```bash
SA_EMAIL=your-cloudrun-sa@$PROJECT_ID.iam.gserviceaccount.com

gcloud run deploy faceless-reels-pipeline \
  --image $IMAGE \
  --region us-central1 \
  --platform managed \
  --memory 4Gi \
  --cpu 2 \
  --timeout 300 \
  --concurrency 1 \
  --service-account $SA_EMAIL \
  --set-secrets "GEMINI_API_KEY=GEMINI_API_KEY:latest,PEXELS_API_KEY=PEXELS_API_KEY:latest"
```

Posting is not part of this deployment. Do not add Cloud Scheduler, Cloud
Tasks, or a Cloud Run job that calls `--publish`.

---

## Output Files

The script command creates `output/{job_id}/script.json` with `"approved": false`.
`--dry-run` adds the media files only after that flag is set to true.

```
output/
  abc12345/
    script.json          # title, narration, pexels_query, caption, approved, ...
    audio.wav            # Kokoro narration (--dry-run)
    timings.json         # word or chunk timings (--dry-run)
    background.mp4       # Pexels clip (--dry-run)
    captions.ass         # burned-in narration cues (--dry-run)
    final_reel.mp4       # 1080x1920 H.264/AAC (--dry-run)
    publish.json         # container_id, post_id, timestamps (--confirm-publish)
```

`OUTPUT_DIR` overrides the directory. The container sets it to `/tmp/output`
so the non-root user can write it. The publish command reads
`$OUTPUT_DIR/<job_id>/script.json` and `$OUTPUT_DIR/<job_id>/final_reel.mp4`.

On Cloud Run, `output/` is ephemeral. The webhook does not upload that
directory anywhere.

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Liveness probe |
| `/run` | POST | Generate one unapproved script and return |
| `/run-async` | POST | Same script generation. Does not queue render or publish |

### Request body (both /run endpoints)

```json
{
  "topic": "string (required)",
  "niche": "niches/example.json",
  "job_id": "string (optional, must match ^[a-z0-9-]{1,32}$)"
}
```

### Response

```json
{
  "job_id": "abc12345",
  "status": "script_ready",
  "approved": false,
  "script_path": "output/abc12345/script.json",
  "message": "Script written with approved=false. Nothing was rendered or published."
}
```

## Niche profile

`niches/example.json` is the checked-in example. Unknown keys are rejected.

| Field | Type | Notes |
|-------|------|-------|
| `name` | string | 1–80 characters |
| `audience` | string | Who the reel is for |
| `tone` | string | How it should sound |
| `language` | string | Default `en` (`en`, `pt-BR`, …) |
| `duration_seconds` | int | 15–90. Render fails if audio exceeds this plus 5s, and never allows more than 90s. |
| `voice` | string | Optional Kokoro voice. Default `af_heart`. |
| `kokoro_lang` | string | Optional Kokoro lang code. Default `a` (American English). |
| `style.do` | string[] | What the script should do |
| `style.dont` | string[] | What the script must not do |
| `hashtag_pool` | string[] | Tags like `#science` |

## script.json

Gemini returns the first eight fields in one structured response. This process stamps the rest. `approved` is not in the model schema. The writer always saves `false`; a reviewer can set `true` and the file still loads.

| Field | Source |
|-------|--------|
| `title`, `hook`, `on_screen_hook`, `narration`, `pexels_query`, `caption`, `hashtags`, `claims_to_verify` | Gemini |
| `approved` | Writer saves `false`. Reviewer may set `true`. |
| `topic`, `niche`, `language`, `duration_seconds`, `voice`, `kokoro_lang` | Copied from the request, niche, and Kokoro settings |

`claims_to_verify` is the human fact-check list. The script is not retrieval-grounded.

## Tests

```bash
python -m pytest tests
```

---

## Instagram Access Token

`--confirm-publish` sends the token as `Authorization: Bearer` on Graph API
calls and as `Authorization: OAuth` on `rupload.facebook.com`. It never
prints the token.

Resumable upload is documented for apps that use **Facebook Login for
Business**, on `graph.facebook.com`. An Instagram Login token
(`graph.instagram.com`) is a different host and is not what this command
calls.

The content-publishing reference asks for a Facebook user access token with:

- `instagram_basic`
- `instagram_content_publish`
- `pages_read_engagement`

A standard long-lived user token expires after about 60 days. A Business
Manager system user can mint a non-expiring token with those permissions.
Whether a system-user token satisfies the current "Facebook Login for
Business" resumable-upload requirement depends on the app's login setup.
If Meta rejects the token, the command prints the Graph `error` object
(`message`, `type`, `code`, `error_subcode`, `fbtrace_id`).

Set the token only in the environment where you run `--confirm-publish`.
Do not commit it.

---

## Publish sequence

With `--confirm-publish`, and only after the local gates pass, the calls are:

1. `GET /{ig-user-id}/content_publishing_limit?fields=quota_usage,config`
   Stop when `quota_usage + 1 >= quota_total`. `quota_total` is taken from
   the response and is never hardcoded.
2. `POST /{ig-user-id}/media` with `media_type=REELS`, `upload_type=resumable`,
   `caption`, `share_to_feed=true`, `is_ai_generated=true`.
3. `POST` the local mp4 to the returned `uri` (`rupload.facebook.com`) with
   `Authorization: OAuth`, `offset: 0`, and `file_size`.
4. `GET /{container-id}?fields=status_code,status` about every
   `PUBLISH_POLL_INTERVAL_SECONDS` (default 60) until `FINISHED`, `ERROR`,
   `EXPIRED`, or `PUBLISH_MAX_WAIT_SECONDS` (default 600). `ERROR` and
   `EXPIRED` fail with the `status` text.
5. `POST /{ig-user-id}/media_publish` with `creation_id`.

Graph API version defaults to `v26.0` (`GRAPH_API_VERSION`).

---

## Security Notes

- **Never commit `.env`** or service-account JSON keys to version control.
- The publish path does not upload the reel to GCS and does not create a
  public URL. The video bytes go to Meta's resumable upload endpoint.
