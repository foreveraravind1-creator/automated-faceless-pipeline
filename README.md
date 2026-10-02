# Automated Faceless Instagram Reels Pipeline

A topic-in engine for faceless Instagram Reels. A niche is a JSON profile
passed in with the topic. The default command writes an unapproved script
and stops. `--dry-run` renders only after a person sets `approved` to true.
It does not upload or publish.

```
Topic + niche JSON --► one Gemini Flash call --► script.json (approved: false)
approved script.json --► Kokoro-82M + Pexels + ffmpeg --► final_reel.mp4
```

| Stage | Role | When it runs |
|-------|------|----------------|
| Script | One Gemini Flash call | `python main.py --topic ... --niche ...` |
| Voice + picture | Local Kokoro-82M and Pexels | `--dry-run`, and only if `approved` is true |
| Render | ffmpeg, 1080x1920 H.264/AAC | same `--dry-run` |
| Publish | Meta | Not implemented. `--publish` is refused. |

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

Only `GEMINI_API_KEY` is required for that command. `--publish` is refused.

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
> - **Timeout**: The Meta polling loop alone allows up to 6 minutes, plus TTS
>   and ffmpeg time. Default 5-minute HTTP timeout causes a 504 mid-pipeline.
>   **Minimum: `--timeout 900` (15 minutes)**
>
> - **Production trigger**: Always use `/run-async` or Cloud Tasks (see below).
>   Never trigger `/run` synchronously from Cloud Scheduler at scale.
>
> Pass 1: both `/run` and `/run-async` only write an unapproved script.
> They do not render, and they do not publish.

### 1. Build and push the container

```bash
PROJECT_ID=your-gcp-project-id
IMAGE=gcr.io/$PROJECT_ID/faceless-reels-pipeline:latest

gcloud builds submit --tag $IMAGE
```

### 2. Create Secret Manager secrets

```bash
for SECRET in GEMINI_API_KEY GOOGLE_API_KEY INSTAGRAM_ACCESS_TOKEN \
              INSTAGRAM_ACCOUNT_ID PEXELS_API_KEY; do
  gcloud secrets create $SECRET --replication-policy="automatic"
done

# Populate each secret with your own values. Never commit them.
echo -n "your-gemini-api-key" | gcloud secrets versions add GEMINI_API_KEY --data-file=-
# Repeat for all others...
```

### 3. Grant IAM roles for Workload Identity + Signed URL signing

The Cloud Run service account needs these roles to generate V4 Signed URLs
without a service-account key file:

```bash
PROJECT_ID=your-gcp-project-id
SA_EMAIL=your-cloudrun-sa@$PROJECT_ID.iam.gserviceaccount.com

# Allow GCS access for a later publish pass. Voice is local Kokoro, not Cloud TTS.
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/storage.admin"

# Allow the SA to sign blobs (needed for V4 Signed URLs on Cloud Run)
# The SA grants this role TO ITSELF - this is the standard pattern.
gcloud iam service-accounts add-iam-policy-binding $SA_EMAIL \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/iam.serviceAccountTokenCreator"
```

### 4. Deploy to Cloud Run

```bash
gcloud run deploy faceless-reels-pipeline \
  --image $IMAGE \
  --region us-central1 \
  --platform managed \
  --memory 4Gi \
  --cpu 2 \
  --timeout 900 \
  --concurrency 1 \
  --service-account $SA_EMAIL \
  --set-secrets "GEMINI_API_KEY=GEMINI_API_KEY:latest,\
GOOGLE_API_KEY=GOOGLE_API_KEY:latest,\
INSTAGRAM_ACCESS_TOKEN=INSTAGRAM_ACCESS_TOKEN:latest,\
INSTAGRAM_ACCOUNT_ID=INSTAGRAM_ACCOUNT_ID:latest,\
PEXELS_API_KEY=PEXELS_API_KEY:latest" \
  --set-env-vars "GCS_BUCKET_NAME=faceless-reels-public-assets,GCS_REGION=US"
```

### 5. Production trigger: Cloud Tasks (recommended) or Cloud Scheduler

**Option A — Cloud Tasks (best for reliability)**

Use Cloud Tasks to enqueue jobs. Each task calls `/run-async` so the HTTP
response is immediate and Cloud Run is not held open for 10+ minutes:

```bash
gcloud tasks queues create reels-queue --location us-central1

# Enqueue a job
gcloud tasks create-http-task \
  --queue reels-queue \
  --location us-central1 \
  --url "https://<your-cloud-run-url>/run-async" \
  --method POST \
  --header "Content-Type:application/json" \
  --body '{"topic":"Why is the sky blue?"}'
```

**Option B — Cloud Scheduler (simple cron)**

```bash
gcloud scheduler jobs create http faceless-reels-daily \
  --location us-central1 \
  --schedule "0 9 * * *" \
  --uri "https://<your-cloud-run-url>/run-async" \
  --message-body '{"topic": "daily science fact"}' \
  --headers "Content-Type=application/json" \
  --time-zone "America/New_York"
```

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
```

`OUTPUT_DIR` overrides the directory. The container sets it to `/tmp/output`
so the non-root user can write it.

On Cloud Run, `output/` is ephemeral. The reel is uploaded to GCS before
the container terminates; local files are discarded.

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

> **IMPORTANT: Use a System User token, not a standard user token.**

Standard long-lived user access tokens expire after **60 days**. A static token
stored in Secret Manager will silently fail after two months.

**Fix: generate a non-expiring token via a Business Manager System User.**

1. Open [Meta Business Manager](https://business.facebook.com) → Settings → Users → System Users
2. Create a System User (Admin role)
3. Assign your Instagram Professional Account to the System User
4. Generate a token with scopes:
   - `instagram_basic`
   - `instagram_content_publish`
   - `pages_read_engagement`
5. Set expiry to **Never** (only available for System User tokens)
6. Store the token in Secret Manager as `INSTAGRAM_ACCESS_TOKEN`

System User tokens do not expire and survive password changes, account
deactivations, and two-factor re-authentication on the human account.

---

## GCS Bucket & Signed URL Security

The pipeline uses **V4 Signed URLs** (30-minute TTL) rather than making objects
or the bucket publicly accessible. This means:

- The bucket `faceless-reels-public-assets` remains **fully private**
- Meta's servers fetch the video via the time-limited URL
- After 30 minutes the URL is invalid; the video is safe in GCS
- No object ACLs are modified; the approach is UBLA-compatible by design

---

## Security Notes

- **Never commit `.env`** or service-account JSON keys to version control.
- On Cloud Run, use **Workload Identity** (no key files). Leave
  `GOOGLE_APPLICATION_CREDENTIALS` empty and grant IAM roles to the SA directly.
- The pipeline does NOT disable UBLA on the GCS bucket. Signed URLs work
  regardless of bucket-level access settings.
