# Deploying engram.devanshitiwari.com

Engram runs on the same server as DevSwarm (165.99.53.30), in the same way: nginx terminates TLS and proxies to the app on localhost, and systemd keeps the app running.

```
browser ──HTTPS──▶ nginx (Let's Encrypt, certbot) ──▶ 127.0.0.1:4810 ── engram-web (systemd, user devdi)
```

| Piece | Where | Copy in this repo |
|---|---|---|
| DNS | Cloudflare: `A engram → 165.99.53.30`, DNS only | n/a |
| Web app | `/etc/systemd/system/engram-web.service` | `deploy/engram-web.service` |
| Reverse proxy | `/etc/nginx/sites-available/engram` | `deploy/nginx-engram.conf` |
| Certificate | `/etc/letsencrypt/live/engram.devanshitiwari.com/`, renewed by `certbot.timer` | n/a |
| Secrets | `/home/devdi/lec_to_course/.env` (never committed) | `.env.example` |

The app binds to `127.0.0.1` only and accepts only the `engram.devanshitiwari.com`, `127.0.0.1` and `localhost` Host headers, so nginx is the single way in.

## Ship a change

```bash
git pull && uv sync && npm ci --prefix tools/mermaid
sudo systemctl restart engram-web
curl -s https://engram.devanshitiwari.com/healthz
```

## Set it up from scratch

```bash
sudo cp deploy/engram-web.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now engram-web

sudo cp deploy/nginx-engram.conf /etc/nginx/sites-available/engram   # or the pre-TLS version from git history
sudo ln -s /etc/nginx/sites-available/engram /etc/nginx/sites-enabled/engram
sudo nginx -t && sudo systemctl reload nginx                          # never reload without the test: this nginx serves other sites
sudo certbot --nginx -d engram.devanshitiwari.com --redirect
```

## nginx settings that matter

- `client_max_body_size 2g` and `proxy_request_buffering off`: lecture videos stream straight through to the app instead of being buffered on disk first.
- `proxy_read_timeout` and `proxy_send_timeout 600s`: a 2 GB upload on a slow connection takes minutes.

## Still to come (M8)

- `engram-worker.service`, which processes the upload queue.
- An HSTS header, once the domain has been stable on HTTPS for a while.
