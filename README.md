# Automated Faceless Instagram Reels Pipeline

A production-ready Python multi-agent system that generates and publishes
60-second educational Instagram Reels from a single topic string.

## Architecture

```
Topic --► Agent 1 --► Agent 2 --► Agent 3 --► Agent 4 --► Instagram
         (Script)    (Media)     (Edit)      (Publish)
```

| Agent | Role | Model | Strategy |
|-------|------|-------|----------|
| 1 | Researcher & Writer | `claude-sonnet-4-5` (extended thinking) | LLM: script + JSON generation |
| 2 | Media Synthesizer | `gemini-2.5-flash` + GCloud TTS + Pexels | LLM: query refinement only |
| 3 | Editor | *(no LLM)* MoviePy / ffmpeg | Pure deterministic code |
| 4 | Publisher | `gemini-2.5-flash` + GCS + Meta API | LLM: caption generation only |

---

## Quick Start (Local)

### 1. Prerequisites

- Python 3.12+
- `ffmpeg` installed and on PATH (`brew install ffmpeg` / `apt install ffmpeg`)
- A GCP project with these APIs enabled:
  - Cloud Text-to-Speech API
  - Cloud Storage API
  - IAM API (needed for V4 Signed URL generation with Workload Identity)

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure secrets

```bash
cp .env.example .env
# Edit .env with your real credentials
```

### 4. Run the pipeline

```bash
# Single run
python main.py --topic "5 fascinating facts about black holes"

# With explicit job ID
python main.py --topic "The science of sleep" --job-id sleep-001
```

### 5. Start the webhook server

```bash
uvicorn webhook.server:app --reload --port 8080
```

Trigger via webhook:

```bash
# Asynchronous (RECOMMENDED for production - returns job_id immediately)
curl -X POST http://localhost:8080/run-async \
  -H "Content-Type: application/json" \
  -d '{"topic": "Why do we dream?"}'

# Synchronous (testing only - blocks until pipeline completes)
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"topic": "Why do we dream?"}'
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

### 1. Build and push the container

```bash
PROJECT_ID=your-gcp-project-id
IMAGE=gcr.io/$PROJECT_ID/faceless-reels-pipeline:latest

gcloud builds submit --tag $IMAGE
```

### 2. Create Secret Manager secrets

```bash
for SECRET in ANTHROPIC_API_KEY GOOGLE_API_KEY INSTAGRAM_ACCESS_TOKEN \
              INSTAGRAM_ACCOUNT_ID PEXELS_API_KEY; do
  gcloud secrets create $SECRET --replication-policy="automatic"
done

# Populate each secret
echo -n "sk-ant-..." | gcloud secrets versions add ANTHROPIC_API_KEY --data-file=-
# Repeat for all others...
```

### 3. Grant IAM roles for Workload Identity + Signed URL signing

The Cloud Run service account needs two specific roles to generate V4 Signed URLs
without a service-account key file:

```bash
PROJECT_ID=your-gcp-project-id
SA_EMAIL=your-cloudrun-sa@$PROJECT_ID.iam.gserviceaccount.com

# Allow GCS and TTS access
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/storage.admin"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/cloudtexttospeech.user"

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
  --set-secrets "ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,\
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

Each run creates `output/{job_id}/`:

```
output/
  abc12345/
    script.json          # Agent 1: structured script payload
    audio.mp3            # Agent 2: TTS narration (en-US-Journey-D)
    background.mp4       # Agent 2: raw Pexels stock footage
    final_reel.mp4       # Agent 3: finished 1080x1920, H.264/AAC, 30fps
```

On Cloud Run, `output/` is ephemeral. The reel is uploaded to GCS before
the container terminates; local files are discarded.

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Liveness probe (no auth) |
| `/run` | POST | Synchronous run (local testing only) |
| `/run-async` | POST | Async dispatch — use this in production |

### Request body (both /run endpoints)

```json
{
  "topic": "string (required)",
  "job_id": "string (optional, auto-generated if omitted)"
}
```

### Response

```json
{
  "job_id": "abc12345",
  "status": "completed | queued | error",
  "instagram_post_id": "17854360229135492",
  "gcs_url": "https://storage.googleapis.com/faceless-reels-public-assets/reels/...",
  "message": "Pipeline completed successfully."
}
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
