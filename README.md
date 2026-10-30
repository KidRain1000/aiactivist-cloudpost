# AI Activist cloud poster

Cloud version of `../ai-activist-autopost/poster.py`. The rules are the same; it runs in GitHub Actions, so posting continues while the Mac is off. Built 10/29. **Not deployed**: no repo, no secrets, nothing posted. Each setup step below needs Chris's OK.

## Files
| File | What |
|---|---|
| `cloudpost.py` | One pass: posts what's due and writes results to `queue.json` and `post-log.csv`. `--dry-run` uses no network and writes nothing. |
| `.github/workflows/post.yml` | Runs every 10 min plus manual dispatch. Concurrency group `cloudpost` (never overlaps). Commits queue and log back. No sleep steps. |
| `tools/export_queue.py` | Mac queue to cloud queue (approved only). Copies the mp4s for the media repo, remuxed to be Reels-safe (needs ffmpeg). Merges and never clobbers cloud state. |
| `tests/test_cloudpost.py` | Offline tests with a fake network: OAuth signature, IG two-phase, X/Bluesky video, caps, stale, heartbeat, export. |
| `queue.json`, `post-log.csv` | Start empty. They live in the poster repo. |

## Rules (same as poster.py)
- Caps per CT day: Bluesky 1, X 8, Instagram 7. Never two posts on one platform within 70 min. Caps count the queue, the log and, for Bluesky, the live feed.
- More than 6h late = `failed` (stale), never posted late. No replies.
- Claim before send: `claimed_at` is saved, **committed and pushed** before the one irreversible call (`createRecord`, `POST /2/tweets`, `media_publish`). If the push fails, nothing is sent. A claimed item found later is resolved:
  - Bluesky checks its pre-chosen record key.
  - Instagram checks the container: `PUBLISHED` = posted, `FINISHED` = safe to publish.
  - X is marked failed for a human check.
- No waiting inside a run (house rule). Slow media is two-phase, and the ids are stored on the item:
  - Instagram: run N creates the container (`ig_container_id`). A later run publishes when `status_code` = `FINISHED`. It never publishes in the run that created the container.
  - X video: INIT/APPEND/FINALIZE (`x_media_id`). A later run checks STATUS and posts once it is `succeeded`. It re-uploads if the upload is more than 20h old.
  - Bluesky video: a video.bsky.app job (`bsky_video_job`). A later run picks up the blob and posts.
  - One item per platform per run, so in-flight items finish first.
- Failures email chris.pearson@savoytx.com via Resend, from noreply@getnmad.com.
- Daily heartbeat: the first run at or after 7 PM CT emails what was posted in the last 24h per platform, failures, and approved items left per platform. The subject starts with `QUEUE RUNNING LOW` when any platform has fewer than 3 days of items left.

## Mac kicker (added 10/30)
GitHub's own cron is unreliable: on 10/30 only 3 of 144 scheduled runs happened, so every X video (needs two runs: upload, then post) went stale. `tools/kick.sh` runs from launchd (`com.tars.aiactivist-cloudkick`, every 10 min, log `~/Library/Logs/aiactivist-cloudkick.log`) and dispatches the workflow when no run is in flight. The GitHub cron (`3-59/10`) stays as the backup while the Mac is off or asleep.
Install: `cp tools/com.tars.aiactivist-cloudkick.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tars.aiactivist-cloudkick.plist`. Stop: `launchctl bootout gui/$(id -u)/com.tars.aiactivist-cloudkick`.

## Cloud item format
Same as the Mac queue, except:
- `video` (filename) + `video_url` (public URL) replace `video_path`.
- `x_link` (default `false`): X text gets no link, because a URL makes the post $0.20 instead of $0.015. An X item whose text contains a URL is refused unless `x_link: true`.
- Images are not supported in the cloud (no approved items use them today).

The poster writes these fields: `posted_url`, `posted_at`, `error`, `attempts`, `claimed_at`, `bsky_rkey`, `bsky_video_job`, `bsky_blob`, `x_media_id`, `x_media_at`, `x_media_ready`, `ig_container_id`. Top-level `heartbeat_sent_on`.

## Setup Chris has to do (each step okayed separately)

### 1. Repos (GitHub, KidRain1000)
- **Media repo, PUBLIC**, e.g. `ai-activist-media`, with the mp4s in `media/`. Turn on **GitHub Pages** (branch `main`, root). `MEDIA_BASE_URL` = `https://kidrain1000.github.io/ai-activist-media/media`.
  - Why Pages and not raw.githubusercontent.com: raw serves mp4s as `application/octet-stream` (checked 10/29). Pages serves `video/mp4`. Meta fetches `video_url` itself; whether it accepts octet-stream is not verified.
- **Poster repo** = this folder as the repo root. **Decision needed: private or public.**
  - Private: GitHub Free includes $12/month of Actions ($0.006/min Linux, about 2,000 min). Every 10 min = 4,320 runs/month, at least 1 billed minute each, so about $26/month gross. Without a payment method and spending limit, Actions **stop when the allowance runs out** (the 5/14 NMAD outage). The allowance is shared with Chris's other workflows.
  - Public: Actions minutes are free. The queue (upcoming post text) and log become public. Secrets stay secret.
  - Or keep it private and change the cron to every 30 min (about 1,440 min/month). Posts then go out up to about 30 to 40 min after the slot.
- Repo settings: Actions > General > Workflow permissions = read and write (the workflow commits back).

### 2. Secrets (poster repo > Settings > Secrets and variables > Actions)
| Name | Kind | From |
|---|---|---|
| `BSKY_APP_PASSWORD` | secret | bsky.app > Settings > Privacy and security > App passwords. A new one, not the Keychain password. |
| `BSKY_HANDLE` | variable | `theaiactivist.com` |
| `X_API_KEY`, `X_API_SECRET` | secret | X developer app (console.x.com), consumer keys |
| `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` | secret | Same app. Access token generated for @aiactivisthq with **Read and write** permission. Regenerate it after switching permissions. |
| `IG_USER_ID` | secret | Instagram business account id: `GET /me/accounts?fields=instagram_business_account` |
| `IG_ACCESS_TOKEN` | secret | Long-lived token (see 4) |
| `RESEND_API_KEY` | secret | The same key as `/Users/cc/Projects/nmad/.env.local` |

### 3. X developer account
- Developer app with **pay-per-use billing** (payment card, prepaid credits). Prices from docs.x.com/x-api/getting-started/pricing (10/29):
  - $0.015 per post; $0.20 per post with a URL.
  - The media upload endpoints have no separate price listed.
  - New accounts get $20 in credits after the first purchase.
- At 8/day that is about $3.60/month without links. Set a spending limit in the console.
- Auth used: OAuth 1.0a user context (keys plus access token for @aiactivisthq).

### 4. Meta / Instagram
- @aiactivisthq is already a business account (10/29). It must be **linked to a Facebook Page**.
- Meta developer app (Business type). Add the Instagram product with **Facebook Login for Business**.
- Permissions: `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`, `pages_show_list`. Add `ads_management` and `ads_read` if Chris's role on the Page comes through Business Manager.
- Token: generate a user token in Graph API Explorer, exchange it for a **long-lived token** (about 60 days), and store it as `IG_ACCESS_TOKEN`.
  - A long-lived user token expires around day 60. Better: a Page access token derived from the long-lived user token (does not expire), or a System User token in Business Manager. Pick one when setting up.
- The app can stay in Development mode if Chris (app admin) is the only account posting. Not verified for content publishing; check during setup.

### 5. Switch over (one platform at a time is fine)
1. `python3 tools/export_queue.py --base-url <MEDIA_BASE_URL> --dry-run`, then without `--dry-run`. This writes `queue.json` here and the mp4s to `export/media/`.
2. Push `export/media/*.mp4` to the media repo. Check one URL loads in a browser.
3. Push this folder to the poster repo. Run the workflow once by hand (workflow_dispatch) with nothing due, and check the log.
4. **Stop the Mac poster for the moved items** (`launchctl bootout gui/$(id -u)/com.tars.ai-activist-autopost`), or set those items to `killed` in the Mac queue. Two posters on the same items = double posts.
5. From then on, new approved items go into the poster repo's `queue.json` (re-run the export: it only adds new ids). Videos go to the media repo first.

## Commands
```bash
python3 "/Users/cc/My Drive (cjp1000@gmail.com)/Rainmaker/Scripts/ai-activist-cloudpost/tests/test_cloudpost.py" -v
python3 "/Users/cc/My Drive (cjp1000@gmail.com)/Rainmaker/Scripts/ai-activist-cloudpost/cloudpost.py" --dry-run --now 2026-10-30T10:20
python3 "/Users/cc/My Drive (cjp1000@gmail.com)/Rainmaker/Scripts/ai-activist-cloudpost/tools/export_queue.py" --base-url https://kidrain1000.github.io/ai-activist-media/media --dry-run
```

## API references (checked 10/29)
- X: `POST https://api.x.com/2/tweets` (docs.x.com/x-api/posts/create-post).
- X media upload, chunked: docs.x.com/x-api/media/quickstart/media-upload-chunked.
  - `POST /2/media/upload/initialize` (JSON: `media_type`, `total_bytes`, `media_category=tweet_video`).
  - `POST /2/media/upload/{id}/append` (multipart: `media`, `segment_index`; at most 5 MB per segment).
  - `POST /2/media/upload/{id}/finalize`.
  - `GET /2/media/upload?command=STATUS&media_id=` (`processing_info.state`: pending, in_progress, succeeded, failed).
  - Upload sessions expire after 86400s.
- Instagram: developers.facebook.com/docs/instagram-platform/content-publishing and .../instagram-graph-api/reference/ig-user/media.
  - `POST /{ig-user-id}/media` (`media_type=REELS`, `video_url`, `caption`, `share_to_feed`).
  - `GET /{container-id}?fields=status_code` (`EXPIRED`, `ERROR`, `FINISHED`, `IN_PROGRESS`, `PUBLISHED`).
  - `POST /{ig-user-id}/media_publish` (`creation_id`).
  - Limits: 100 API posts per rolling 24h (`GET /{ig-user-id}/content_publishing_limit`), 400 containers per 24h, containers expire after 24h.
  - Reels video: MP4/MOV, H.264/HEVC, AAC at most 48 kHz, 23 to 60 fps, 3 s to 15 min, 300 MB maximum, moov atom at the front.

## Not verified (test on the first live run, watched)
- Whether OAuth 1.0a works on the v2 media upload endpoints. The spec lists a generic OAuth user-token scheme next to OAuth 2.0 `media.write`. No one has confirmed a working OAuth 1.0a call publicly. Fallback: an OAuth 2.0 user token with `media.write` (a small code change in `XClient.call`).
- Meta's own acceptance of our mp4s. Checked locally 10/29: all 39 renders failed the Reels spec as rendered (moov atom at the end, AAC 96 kHz). `export_queue.py` now remuxes the media-repo copies (video copied untouched, AAC 48 kHz, +faststart). Afterwards all 39 are H.264 1080x1920 at 30, 29.97 or 60 fps, 7 to 25 s, with moov at the front. A real container create is still the only true test.
- How long a Bluesky video blob stays usable unreferenced between the two runs (normally about 10 min).
- GitHub cron timing: scheduled runs can start late or be skipped under load.
