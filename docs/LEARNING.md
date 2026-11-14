# Decision log

Each entry records what was decided, why, and what else was considered.

## Haiku reads frames, Opus reasons

- **What:** Claude Haiku 5.5, through the Anthropic API, describes every keyframe (structured output, low effort, 8 calls in parallel). Opus, through headless Claude Code on the Claude plan, writes chapters, notes, diagrams, flashcards, the quiz and the narration script. It reads a compact text timeline and opens only the frame images it relies on.
- **Why:** Recall measured both on a 58-minute lecture with 156 frames. Haiku took 69 s and cost about $0.15. Opus took about 3 minutes and 633k input tokens (about $5.90 at API prices), so roughly 40× more. Opus explained diagrams better, and it still does that here, but only for the frames that end up in the course.
- **Alternatives:** Opus reads every frame (too much usage). Everything on `claude -p` (no API key needed, but Haiku through Claude Code carries about 19k tokens of overhead per session unless it's stripped). This remains the fallback when no API key is set.

## Opus never sees an API key

- **What:** The Claude Code subprocess gets an environment with `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY` and other secrets removed.
- **Why:** If Claude Code finds `ANTHROPIC_API_KEY`, it bills the API instead of the plan. This is Recall's `claude_env()`.

## Anchors, not model-written timestamps

- **What:** Opus cites each item as `{"frame": "t01878"}` or `{"quote": "words the lecturer said"}`. Code resolves the anchor to a timestamp and thumbnail (a frame lookup, or a letter-trigram match against the transcript).
- **Why:** Models are bad at producing exact times, but good at quoting. A failed match is detected and flagged, instead of silently pointing to the wrong moment. Taken from Recall's `pipeline/anchor.py`.

## Keyframes: Recall's dHash method plus whiteboard handling

- **What:** Sample every 2 s at full resolution. Keep a frame on a dHash jump of at least 18 bits, every 60 s, and just before each change. Drop black frames. Use Tesseract text for backward consolidation. For whiteboards, add erase detection, a sharpness score and a penalty for frames where a person blocks the board.
- **Why:** Recall's method is proven on screen recordings and slides (1117 → ~155 frames for 37 minutes). Whiteboards fill up gradually, so a hash threshold alone either keeps every stroke or misses the finished board.

## DevSwarm's stylesheet, copied unchanged

- **What:** `engram/web/static/theme.css` is a byte-for-byte copy of DevSwarm's `dashboard.css`. Engram's own components live in `engram.css` and use only its tokens.
- **Why:** The two products should look like one family. Never editing the copy means it can be refreshed from DevSwarm at any time, and a reviewer can confirm it matches with one `diff`.
- **Alternatives:** Porting the tokens into a new stylesheet (it would drift), or a shared package (too much machinery for two apps).

## Upload form hidden until the pipeline exists

- **What:** The home page shows the upload form only when `accept_uploads` is on *and* at least one access code is set.
- **Why:** A showcase should never offer a button that leads nowhere. Until M6, it says plainly that uploads are off and points to the sample courses.

## Port 4810

- **What:** `engram serve` defaults to `127.0.0.1:4810`.
- **Why:** Port 4700 is already in use on the host machine. Binding to localhost only means the Cloudflare Tunnel (M8) is the single public way in.

## The name: Engram

- **What:** The project was called Lectern while it was planned, and renamed Engram before M1.
- **Why:** An engram is the physical trace a memory leaves in the brain, which is what the product makes from a lecture. It's one short word and easy to say. Other projects use the name (DeepSeek's Engram memory architecture, and an agent-memory tool), so on GitHub it is always shown with the owner, `Devanshi-cmd/engram`. The package is never published to PyPI, where `engram` is already taken.
