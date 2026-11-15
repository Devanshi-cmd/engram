# Deploying engram.devanshitiwari.com

Engram runs on the same server as DevSwarm (165.99.53.30), in the same way: nginx terminates TLS and proxies to the app on localhost, and systemd keeps the app running.

```
browser ──HTTPS──▶ nginx (Let's Encrypt, certbot) ──▶ 127.0.0.1:4810 ── engram-web (systemd, user devdi)
                                                                              │ queued jobs (SQLite)
                                                                              ▼
                                                                         engram-worker (systemd, user devdi)
```

| Piece | Where | Copy in this repo |
|---|---|---|
| DNS | Cloudflare: `A engram → 165.99.53.30`, DNS only | n/a |
| Web app | `/etc/systemd/system/engram-web.service` (with `--uploads`) | `deploy/engram-web.service` |
| Worker | `/etc/systemd/system/engram-worker.service` | `deploy/engram-worker.service` |
| Reverse proxy | `/etc/nginx/sites-available/engram` | `deploy/nginx-engram.conf` |
| Certificate | `/etc/letsencrypt/live/engram.devanshitiwari.com/`, renewed by `certbot.timer` | n/a |
| Secrets | `/home/devdi/lec_to_course/.env` (never committed) | `.env.example` |

The app binds to `127.0.0.1` only and accepts only the `engram.devanshitiwari.com`, `127.0.0.1` and `localhost` Host headers, so nginx is the single way in.

## Ship a change

```bash
git pull && uv sync && npm ci --prefix tools/mermaid
sudo systemctl restart engram-web engram-worker   # the worker's current job resumes
curl -s https://engram.devanshitiwari.com/healthz
```

## Set it up from scratch

```bash
sudo cp deploy/engram-web.service deploy/engram-worker.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now engram-web engram-worker

sudo cp deploy/nginx-engram.conf /etc/nginx/sites-available/engram   # or the pre-TLS version from git history
sudo ln -s /etc/nginx/sites-available/engram /etc/nginx/sites-enabled/engram
sudo nginx -t && sudo systemctl reload nginx                          # never reload without the test: this nginx serves other sites
sudo certbot --nginx -d engram.devanshitiwari.com --redirect
```

## nginx settings that matter

- `client_max_body_size 2g` and `proxy_request_buffering off`: lecture videos stream straight through to the app instead of being buffered on disk first.
- `proxy_read_timeout` and `proxy_send_timeout 600s`: a 2 GB upload on a slow connection takes minutes.
- `add_header Strict-Transport-Security "max-age=31536000" always`: browsers remember to use HTTPS for a year, as the other sites on this server do. It covers this host only. `includeSubDomains` would commit every `*.devanshitiwari.com` site to HTTPS too, and those are separate projects.

## Cloudflare R2 (big files)

Uploads, web copies and summary videos live in an R2 bucket, so the server's disk only holds the lecture the worker is processing (deleted when the job ends), frames, transcripts and the database. Until the four `R2_` values in `.env` are filled in, everything stays on disk.

1. In the Cloudflare dashboard: **R2 → Create bucket** (for example `engram-lectures`). Keep it private: pages play files through signed links that last 6 hours.
2. **R2 → Manage API tokens → Create API token**, permission **Object Read & Write**, limited to that bucket. Copy the Access Key ID and Secret Access Key.
3. Fill in `.env`: `R2_ACCOUNT_ID` (shown on the R2 overview page), `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`.
4. Run `uv run engram r2-check`. It writes, reads and deletes a test object, then tries to set the bucket's CORS rule. An object-only token can't set CORS; if it says so, add this rule under the bucket's **Settings → CORS policy**:

   ```json
   [{"AllowedOrigins": ["https://engram.devanshitiwari.com"], "AllowedMethods": ["PUT", "GET", "HEAD"], "AllowedHeaders": ["content-type"], "MaxAgeSeconds": 3600}]
   ```

5. `sudo systemctl restart engram-web engram-worker`.

Optional: an R2 lifecycle rule that deletes `uploads/` objects older than a day if no job claimed them (someone with a code who started an upload and closed the tab).

How uploads work: the page sends the access code, the file's name and its size first. Only if the code is right does the server create a one-time upload slot (an hour) and answer with a signed PUT URL for exactly that key, size and type. The browser sends the file straight to R2, and the server never holds it. Then the page reports "done"; the server checks that the object arrived complete and queues the job. Ten wrong codes from one address in 10 minutes block it for the rest of that window.

YouTube links: the page sends the access code and the link. The server checks the code and its daily allowance, then reads the video's details with yt-dlp (title, length, public or not) and refuses private, members-only, age-restricted and live videos, and anything over the length limit, before a job exists. The worker downloads the video when the job runs (up to 720p, H.264 and AAC, so it plays as it is), puts it in R2 like an upload, and deletes the local copy afterwards. The same video sent again reuses its job.

- **Keep yt-dlp current.** YouTube changes often and old versions stop working: `uv lock --upgrade-package yt-dlp && uv sync`, then restart both services. yt-dlp uses Node (already installed for Mermaid) to solve YouTube's player challenges.
- **If YouTube blocks the server** ("confirm you're not a bot"), links fail with a message that says to upload the file instead. Uploads keep working.

## The worker

- Runs as `devdi`, so headless Claude Code uses that account's login (the Claude plan). Check with `sudo -u devdi claude -p "hi"` if Opus stages fail with a login error.
- Needs `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` and `ANTHROPIC_API_KEY` in `.env`, ffmpeg, and Node with `tools/mermaid` installed.
- `PrivateTmp=true`: the worker can't see `/tmp`. Uploads live in `.data/uploads/`, so that's fine, but a job made with `engram run /tmp/…` from a shell can only be processed by that shell.
- Logs: `journalctl -u engram-worker -f`. List jobs with `engram jobs`; show a course on the home page with `engram list <job>` (and `--hide`).
- Delete jobs with `engram delete <job>…`: their folders, their files in R2 (an upload shared with another job is kept) and their records. Listed or running jobs are refused. Cached transcripts and readings stay, so the same lecture uploaded again is free. Upload records stay too: they count toward their code's daily limit.

## Limits

- **Per access code:** `ENGRAM_DAILY_UPLOADS` lectures per 24 hours (default 3). A code over its limit gets no upload slot, so nothing is sent; the message says how many hours until it frees up. Codes are counted by a hash, never stored.
- **Per lecture:** `ENGRAM_MAX_MINUTES` (default 120). Checked before processing starts, so an over-long lecture costs nothing.
- **Queue:** 5 lectures waiting at most; more are turned away with "try again in an hour".

## Sample courses (the gallery)

Only openly licensed lectures go on the home page, each credited on its course page:

```bash
uv run engram run lecture.mp4 --title "…"
uv run engram list <job> --source "MIT OpenCourseWare" --url "https://…" \
    --license "CC BY-NC-SA 4.0" --license-url "https://creativecommons.org/licenses/by-nc-sa/4.0/"
```

`--notice` adds a line a licence asks for (Khan Academy's: "All Khan Academy content is available for free at www.khanacademy.org"). The course page says the course is made from the lecture and shared under the same licence, as ShareAlike requires. The demo is free with no ads, which keeps it within NonCommercial. Stanford CS229 isn't openly licensed: it stays unlisted.

A course's picture on the home page and in link previews is its summary video's poster. To use a frame of your choice instead:

```bash
ffmpeg -ss 6:45 -i lecture.mp4 -frames:v 1 still.png   # a frame at full resolution
uv run engram cover <job> still.png                    # saved as cover.jpg; --remove undoes it
```
