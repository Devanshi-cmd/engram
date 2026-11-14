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

## The worker

- Runs as `devdi`, so headless Claude Code uses that account's login (the Claude plan). Check with `sudo -u devdi claude -p "hi"` if Opus stages fail with a login error.
- Needs `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` and `ANTHROPIC_API_KEY` in `.env`, ffmpeg, and Node with `tools/mermaid` installed.
- `PrivateTmp=true`: the worker can't see `/tmp`. Uploads live in `.data/uploads/`, so that's fine, but a job made with `engram run /tmp/…` from a shell can only be processed by that shell.
- Logs: `journalctl -u engram-worker -f`. List jobs with `engram jobs`; show a course on the home page with `engram list <job>` (and `--hide`).

## Still to come (M8)

- Daily limits per access code, and a queue position on the home page.
- An HSTS header, once the domain has been stable on HTTPS for a while.
