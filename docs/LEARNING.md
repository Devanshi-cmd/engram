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
