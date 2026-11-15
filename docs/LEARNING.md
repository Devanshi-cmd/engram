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
- **Why:** Port 4700 is already in use on the host machine. Binding to localhost only means nginx (M8) is the single public way in.

## The name: Engram

- **What:** The project was called Lectern while it was planned, and renamed Engram before M1.
- **Why:** An engram is the physical trace a memory leaves in the brain, which is what the product makes from a lecture. It's one short word and easy to say. Other projects use the name (DeepSeek's Engram memory architecture, and an agent-memory tool), so on GitHub it is always shown with the owner, `Devanshi-cmd/engram`. The package is never published to PyPI, where `engram` is already taken.

## M1: a content-hashed stage DAG instead of "skip if the file exists"

- **What:** Each stage's cache key hashes its name, version, parameters, the source video, and the output digests of its dependencies. A stage is skipped only if the key matches *and* its outputs still hash the same.
- **Why:** Recall skips a step when its output file exists. That's simple, but a prompt change or an edited file is silently ignored, and the only way to redo a step is a manual `--force`. Hashing makes the cache correct by construction. Bumping a stage's version re-runs exactly that stage, and downstream stages re-run only if its output bytes really changed.
- **Alternatives:** A workflow engine (Prefect, Dagster, Celery). That's heavy machinery for one worker on one machine, and it would hide the part worth showing in an interview.

## Paid results cached across jobs

- **What:** Transcripts are stored by the hash of the audio bytes, the transcriber and the key terms, outside any job folder.
- **Why:** Scribe isn't deterministic (two runs on the same 6-minute clip gave 742 and 740 words), and every call costs credits. Re-uploading a lecture, or deleting a job, should never pay twice.

## Bit-exact audio extraction

- **What:** `ffmpeg -map_metadata -1 -fflags +bitexact -flags:a +bitexact`.
- **Why:** Found in the M1 demo. A re-muxed copy of a lecture carried a `comment` tag, ffmpeg copied it into the MP3, the audio hash changed, and Scribe was paid again for identical speech. Without tags and with a bit-exact encode, the same speech always gives the same bytes. A test re-tags a video and checks the audio hash doesn't change.

## A short-lived SQLite connection per call

- **What:** `Store` opens a connection per operation, in WAL mode with a 30 s busy timeout.
- **Why:** Stages run in threads, and the web app will read while the worker writes. A shared connection needs locking; per-call connections in WAL mode give concurrent readers and serialised writers for free. The overhead (well under a millisecond) is nothing next to ffmpeg or an API call.

## Readable errors, full tracebacks on disk

- **What:** `EngramError` subclasses carry a sentence for the job page ("This file has no audio stream. Engram needs to hear the lecture."). ffmpeg failures quote the tool's own last stderr line. Everything else is shown as `Type: message`, with the traceback in the job's `engram.log`.
- **Why:** Recall's lesson: a person retrying a job needs one line that says what went wrong, not a stack trace.

## Filler fragments wait for their sentence

- **What:** A segment of fewer than 4 words doesn't end at a pause of up to 5 s. It waits for the rest of its sentence. (Recall used 2 s.)
- **Why:** On the full CS229 lecture, a lecturer thinking at the board produced segments like "um," and "which is" on their own. With a 5 s wait, 689 segments became 674, and "um, which is called the learning rate." reads as one line. Re-segmenting reused the paid transcript and took 0.2 s.

## M2: keyframes for a camera that follows the lecturer

The test lecture (CS229 Lecture 2) is the hard case: the camera pans to follow Andrew Ng almost constantly, he stands in front of the board, and there are hard cuts to plotted slides. Each version below was measured on the full 78 minutes, and each failure shaped the next.

| Version | Keyframes | What was wrong |
|---|---|---|
| Recall's whole-frame hash | n/a | The camera never stops moving, so every frame "changes" |
| Ink + pan + "single later frame covers it" | 219 | Matching found its points on Andrew's shirt rather than the board, so repeated views of a board were never matched |
| Matching on the writing only, with later frames *together* covering an earlier one | 216 | "Person" boxes sometimes covered 100% of the frame (his outline touched the writing), so frames lost all ink and looked like wipes |
| Person = tall *columns* of the shape that stands on the floor | **120** | A good result: distinct boards, the finished state of each, slides kept |

Smaller lessons along the way:

- **Track the camera on the writing, not the frame.** Phase correlation on whole frames tracks the lecturer (the camera follows him too). Painting him out doesn't help: the painted patch has edges that move with him. Ink is fixed to the board, so its shift is the camera's.
- **Writing looks like motion.** New letters shift the ink pattern. Only a moving board (its pixels change, the person excluded) can be a pan, and the match must be confident.
- **A plot isn't a person.** The first person rule ("any tall connected shape") deleted a slide's plot, axes and all. People stand on the floor: their shape reaches the bottom of the frame.
- **The finished board wins ties.** Within 5%, a later frame replaces an earlier one, so a line half-written at 33 s loses to the same line finished at 39 s.
- **Revisits go both ways.** Going backwards keeps the finished board. Going forwards drops a return visit that shows nothing new.
- **"Erase" was the wrong name.** Most sudden ink drops are the lecturer or a zoom hiding the writing, not a wipe. The view should still end, but the label is now `change`.
- **Not done: a clean-board composite.** Merging frames to remove the lecturer entirely doesn't work here, because the camera follows him and he stays in the middle of the shot. A board mosaic across pans is possible future work.
- **Tests draw their own lecture.** `tests/synthetic.py` renders a whiteboard, a lecturer-shaped figure, a pan, a wipe and a cut, so every rule has a known answer.

## Speech goes to the frame on screen at the word's midpoint

- **What:** Views tile the lecture with no gaps, and each word belongs to the view containing its midpoint.
- **Why:** A word straddling a cut belongs where most of it was said, and there are no unassigned words. On CS229, the span for the gradient-descent frame (24:51–25:33) holds the exact sentence describing what's on the board: "theta J minus alpha … the learning rate … the partial derivative of the cost function J of theta with respect to theta J".

## Hosting: nginx on the same server as DevSwarm, not a tunnel

- **What:** engram.devanshitiwari.com points at this machine (165.99.53.30). nginx terminates TLS (Let's Encrypt via certbot) and proxies to `127.0.0.1:4810`. systemd runs the web app and the worker.
- **Why:** The plan assumed a Cloudflare Tunnel, but this machine already has a public IP and serves DevSwarm exactly this way. Reusing a setup that already works means one less moving part. Uploads need `client_max_body_size` raised and generous proxy timeouts, which a tunnel would have limited anyway.
- **Deployed early (after M2):** the site went live before the product was finished, so every milestone ships to the real URL and deployment problems surface early instead of on launch day. The upload form stays hidden until the worker exists.

## M3: Haiku reads, Opus checks only what's flagged

- **Measured on CS229:** Haiku read 118 frames in 58 s for $0.066. Given Andrew Ng's words, it read f0044 as *"Start with some θ (Say θ = 0⃗) / Keep changing θ to reduce J(θ) / θⱼ := θⱼ − α ∂/∂θⱼ J(θ)"*, including the "learning rate" arrow pointing at α, and flagged the cut-off writing at the bottom itself.
- **Opus as the checker:** given Haiku's reading of the flagged frame f0046, Opus opened the image (16 s, 6.5k tokens) and found that a second equation line was a phantom: the same formula with the handwritten ∂ read as "2".
- **That mistake became a prompt rule (v2):** "a curly ∂ often looks like a 2 or a 6, and θ like a 0; give one reading per line, never alternatives". Re-reading every frame cost $0.066 (cached readings made the prompt change the only thing that re-ran), and the phantom line is gone.

## Trimming headless Claude Code from 43k to 1.8k tokens per call

- **What:** `--system-prompt` replaces Claude Code's default prompt, with `--tools` limited to Read (or none), and no MCP servers, settings, skills or saved session.
- **Why:** a bare `claude -p "Say hi"` used about 43,000 input tokens (21k of them cache writes): Claude Code's coding-agent prompt and tool definitions. Engram needs none of that. Recall measured the same (about 19k → about 740 tokens). `--bare` would trim it too, but it only authenticates with an API key, which would bill the API instead of the plan.

## Tests must never call real Claude

- **What:** an autouse fixture makes `claude_binary()` raise in every test. The test services use `FakeFrameReader`.
- **Why:** after `read_frames` was added, the suite went from 9 s to 45 s. The pipeline tests had no frame reader, fell back to Claude Code, and were calling the real CLI on the plan. Now any test that tries fails loudly.

## Dropped: Tesseract OCR

- **What:** the plan had Tesseract as a hint for Haiku and for consolidation. It's not used.
- **Why:** M2's consolidation compares *ink* after alignment, which works on handwriting where OCR words don't. Haiku reads printed slides on its own. One less system dependency.

## M4: Opus writes, code checks, Haiku judges

- **What:** four stages (`chapters`, `write`, `check`, `course`). Opus plans chapters and writes each chapter's notes, key terms, diagrams, flashcards and quiz. Code resolves every anchor, parses every diagram and assembles the course. Haiku checks every card and question against the lecture.
- **Why this split:** anything that can be checked by code is (stamps, frame ids, quotes, Mermaid syntax). The judgement calls go to two different models, so the one that wrote a quiz question isn't the one deciding it's supported.

## Chapters in one pass, map-reduce only past 150 KB

- **What:** the plan said map-reduce over 15-minute windows. Engram sends the whole timeline in one call when it fits in 150 KB (about 40,000 tokens), and uses map-reduce only beyond that.
- **Why:** a two-hour lecture's timeline is about 150 KB, which Opus reads easily, and seeing the whole lecture gives better boundaries than merging windows that each saw a quarter of it. One call is also cheaper and quicker (15 s on CS229). The map-reduce path exists and is tested for longer lectures.
- **Boundaries are checked, not trusted:** each `starts_at` must parse as a stamp. It is snapped to the nearest real timeline line, chapters under a minute are merged into the previous one, anything past the end is dropped, and the first chapter starts at 0:00.

## Prompt on stdin, tool calls from the event stream

- **What:** `run_claude_code` sends the prompt on stdin and reads `--output-format stream-json`.
- **Why stdin:** Linux limits a single command-line argument to 128 KB, and a timeline can be bigger. A 160 KB test prompt worked on stdin.
- **Why the stream:** the stream shows every `Read` call, so Engram records which frames Opus actually opened. Asking the model to list them would be self-reporting.

## Anchors match words, not segments

- **What:** a quote is matched against 60-word windows (stride 30) by letter trigrams, then refined to the word where it starts: best match over exactly the quote's length, then over two extra words (for fillers the timeline dropped), then the earliest.
- **Bugs found while testing:** ties first went to the *latest* match ("this is data from Portland, Oregon" is said at 02:12 and again at 23:21), and the refinement started two words early because a window starting before the quote still contains all of it. Both have tests now.
- **Own chapter first:** an item's quote is looked up inside its chapter before the whole lecture, because lecturers repeat themselves.
- **Nothing invented:** a weak quote falls back to the frame anchor. A made-up frame id is ignored. An item that can't be placed gets no time and is listed under `unplaced`.

## Mermaid checked by Mermaid

- **What:** `tools/mermaid` runs Mermaid 11.4.1's own `parse()` in Node, with jsdom giving it the DOM it needs to load. One Node process checks all of a chapter's diagrams (about 0.8 s).
- **Alternatives:** a regex "linter" would miss real errors. `mmdc` (Mermaid CLI) needs a headless Chromium. Checking with the same version the course page will load means "parses here" is "renders there".
- **On failure:** one repair call with the parser's error message, then the diagram is dropped and recorded under `dropped_diagrams`. A broken diagram is never shown.

## The grounding check uses a different model and real passages

- **What:** for each card and question, code collects the speech from 40 s before to 70 s after its anchor, the speech around its quoted evidence, and the writing on frames on screen then. Haiku returns `supported`, `unsupported` or `ambiguous`. Only `supported` stays. A missing verdict counts as unsupported.
- **Why Haiku:** a different model than the writer is a fairer judge, and the check is reading comprehension over a short passage, which Haiku does well at a fraction of a cent per chapter.
- **Removed items are kept**, with the verdict and reason, under `removed` in `course.json`. The M7 eval can then measure the judge itself.

## Opus's corrections are part of the output

- **What:** each chapter returns `corrections`: frames Opus opened whose Haiku reading was wrong. On CS229 that was 20 corrections across 41 opened frames, and some changed the content: Haiku had read "Sigmoid / logistic regression" on the outline board, which says "Supervised learning → Regression"; it read an empty circle where the board says $\nabla_\theta J(\theta) = \vec 0$; and it wrote $X^T\theta$ for $X\theta$. Opus wrote the notes from the image, not the reading.
- **Why keep them:** they show the two-model design working, they are the raw material for the M7 handwriting eval, and a future version could feed them back into the timeline.

## M4 on CS229 (78 min)

| Stage | Time | What happened |
|---|---|---|
| `chapters` | 15 s | 1 Opus call, 36k tokens in. 9 chapters that match the lecture's real structure, from "Supervised learning and the hypothesis" to "Deriving the normal equations" |
| `write` | 124 s | 9 Opus calls, 4 at a time (6 min 40 s of Opus time). 232k tokens in, 47k out. 41 frames opened, 20 readings corrected |
| `check` | 16 s | Haiku checked 78 cards and questions for $0.014. All supported |
| `course` | 1 s | 106 notes, 8 diagrams, 50 cards, 28 questions. Every one of 228 anchors matched a quote exactly; none unplaced |

- **Plan usage:** about $2.20 of API-equivalent Opus tokens, billed to the plan, not the API. The only real cost of M4 is the judge's $0.014.
- **Diagrams:** all 8 parsed first time (the prompt's rules: quoted labels, Unicode not LaTeX). The repair path is tested with fakes.
- **Is the judge just lenient?** Zero removals needed checking. Six planted items on chapter 5: the real question and card (supported), the same question with a wrong answer marked, a true fact the lecture never mentions (Cauchy, 1847), a card contradicting the lecture, and a question with two correct options. Haiku got all six right, the last as `ambiguous`, for $0.001. M7 turns this into a proper eval.

## M5: the summary video

- **What:** three stages (`script`, `narrate`, `video`). Opus writes and picks frames, ElevenLabs speaks in the configured voice, and Engram draws every frame itself.
- **Measured on CS229:** script 42 s (2 Opus calls, see below), narration 3 s for 1,040 characters, both videos 112 s. The result is 79 s long: 7 scenes, 183 words, 30 MB for the two files.

## Opus writes from the course, not the timeline

- **What:** the script prompt gets the course outline (chapters, headings, key terms) and the frames the course already uses, about 8 KB, instead of the 97 KB timeline.
- **Why:** the course is the lecture already distilled and checked, so the summary can't drift from it. It's also a small, fast call (26 s). Opus opened 12 thumbnails to choose its frames.

## Code enforces the length, because the prompt didn't

- **What:** the script is counted. Over 200 words, Opus gets one "shorten this, cut words not ideas" call.
- **Why:** asked for 170-210 words, Opus wrote 246 (a 108 s video). Asked for 150-190, it wrote over 200 again. The shortening pass got 183. The voice reads about 2.4 words a second, so 200 words is about 85 s.

## Frames drawn in Python, not ffmpeg filters

- **What:** each output frame is composed with OpenCV and Pillow and piped raw to ffmpeg, which only encodes.
- **Alternatives:** `zoompan` (rounds to whole pixels, so slow moves judder, and it can't crossfade between shots by itself), `xfade` chains and an ASS subtitle file (workable, but captions styled by libass can't match the web app's pills and type, and word-by-word highlighting means one event per word).
- **Cost:** 11-21 ms per frame on this machine. A 79 s video in both shapes takes about two minutes.

## Ken Burns towards the ink, slides shown whole

- **Focus:** the move ends on the centre of mass of the board's ink (black-hat and top-hat, as in M2), so it drifts towards the writing, not the lecturer.
- **Bars:** some CS229 frames are 4:3 slides inside a 16:9 recording. Zooming showed the black bars. Bars are now trimmed, and a trimmed slide whose shape doesn't fit the space is shown whole on the page colour.
- **Two bugs on the way:** the "Stanford" watermark overlaps the right-hand bar, so "a column is a bar if its brightest pixel is dark" kept that bar. A column now counts as content only if more than 8% of it is lit. And fitting (rather than cropping) was first applied to every frame whose shape differed, which put page-coloured bars around 16:9 boards in the portrait's 4:3 card. Only letterboxed slides are fitted now. All three trimmed frames out of 120 are real slides.
- **No AI upscaling:** 720p frames are scaled to 1080p with bicubic interpolation, which invents nothing on the board.

## Captions by sentence, then balanced

- **What:** words are split at sentence ends and pauses over 0.35 s first. A long sentence is then cut into the fewest pieces that fit, of even length.
- **Why:** greedy line-filling put the end of one sentence with the start of the next ("regression. The hypothesis is…") and left single orphaned words. The same chunks are written as WebVTT for the web player.

## Inter for Apple's SF

- **What:** Inter 4.0 (SIL Open Font Licence) is bundled in `engram/render/fonts`.
- **Why:** the web app uses the system font stack, which is SF on Apple devices, but the server has no SF and its licence forbids bundling it. Inter was designed for the same job and is the usual open stand-in.

## Tests render thumbnails

- **What:** an autouse fixture makes the pipeline tests render 320×180 and 180×226 at 10 fps. One test renders real-size frames and checks the layout (caption pill, rounded card corners, page colour around slides).
- **Why:** every pipeline test now ends in a video. At full size that would add minutes to the suite; at thumbnail size it's the same code for 1% of the pixels.

## M6: the web app

- **What:** uploads with an access code, a queue run by `engram worker`, a live job page, the course page (player, transcript, chapters, notes, diagrams, flashcards, quiz, summary) and downloads (Markdown zip, Anki, print/PDF).
- **Measured live:** a 4-minute MIT 18.06 clip uploaded through https://engram.devanshitiwari.com went from upload to finished course in about 4 minutes on the production worker (systemd, Opus through Claude Code on the plan): 2 chapters, 9 notes, 1 diagram, 6 cards, 2 questions, a 29 s summary.

## A separate worker process

- **What:** the web app only records an upload as a queued job. `engram worker` (its own systemd service) runs jobs one at a time.
- **Why:** a lecture takes minutes. In the web process, every deploy would kill whatever was running, and one long job would compete with page requests. As a separate process, a restart of either side loses nothing: the worker requeues anything it left running, and the runner resumes from the last finished stage.
- **One at a time:** the bottlenecks (the Claude plan's limits, ElevenLabs, CPU for video) are shared, so parallel jobs would only slow each other down. Inside a job, stages already run in parallel.
- **Alternatives:** Celery or RQ (a broker to run and monitor for a queue that never holds more than a few jobs); the SQLite store already has the state, so polling it every 2 s is enough.

## A web copy only when needed

- **What:** a `playback` stage. An H.264/AAC MP4 upload is served as it is; anything else (AV1, HEVC, MKV, WebM) gets an H.264 copy at most 720p, CRF 23.
- **Why:** CS229 came from YouTube as AV1, which older Safari can't play, and an iPhone upload would be HEVC, which most non-Apple browsers can't. The copy took 11.5 min for the 78-minute CS229 (502 MB) and runs alongside the other stages.

## Maths found on the server, typeset in the browser

- **What:** the server finds maths by Pandoc's rules and wraps it in `<span class="tex">`; the browser runs KaTeX on those spans only.
- **Why:** the first version used KaTeX's auto-render, which pairs any two dollar signs. In CS229's notes, "price in $1000s): $2104 → 400$" became garbled maths. Pandoc's rules (an opening `$` must be followed by a non-space; a closing one must follow a non-space and not be followed by a digit) leave currency alone, and all 416 maths spans in CS229 now render with no errors. The Anki export uses the same rules.

## Vendored KaTeX and Mermaid, no CDN

- **What:** KaTeX 0.16.11 (woff2 fonts only) and Mermaid 11.4.1 are served from `static/vendor/`.
- **Why:** the Content-Security-Policy allows scripts only from the site itself, so text on a slide can never become code. A CDN would have to be allowed too. Mermaid is the same version as the server-side checker, so a diagram that parsed in M4 renders here. Mermaid needs no `eval`, so `script-src 'self'` holds; styles must allow `'unsafe-inline'` because both libraries position output with style attributes.

## Small things found by looking at the page

- **The same board twice:** Opus can pick the same frame for two sections of a chapter. The page shows it once.
- **The voice ID on a public page:** the job page's activity log showed the narrator's name, which included the ElevenLabs voice ID. The message now says "narration".
- **A short lecture got a long summary:** the 4-minute clip got an 81 s summary. The script's length now scales with the lecture: about 12 words per lecture minute, between 60 and 190 words (25 to 80 s). The clip's summary is now 29 s; CS229's is unchanged.
- **Re-uploading an old video:** a job finished before the course stages existed would have been reused as "done" with no course. It's now queued again to catch up.

## Courses unlisted by default

- **What:** a course page is reachable by its link (a random 12-character id). Only courses marked with `engram list` appear on the home page.
- **Why:** an uploader's lecture isn't the operator's to publish, and lectures like CS229 aren't openly licensed. The sample gallery (M8) will list only lectures with a licence that allows it.

## M7: answer keys made by a person, not a model

- **What:** the handwriting and board-state evals score Engram against labels Devanshi made by hand, in a private labelling page (a claude.ai artifact with its own small database), then exported to `evals/labels/`. Engram's own readings were hidden on that page, so they couldn't anchor the answers.
- **Why:** labels drafted by a Claude model would share the blind spots of the Claude models being measured. The grounding eval needs no labels: it plants faults in real items.

## The grounding eval found a real weakness

- **Method:** every real flashcard and quiz question from the CS229 and MIT 18.06 courses is judged as it is (it should stay), and again with a planted fault: the wrong answer marked, or a card's answer swapped with the next card's in the same chapter.
- **First run:** all 86 real items kept, all 30 wrong answers removed, but 4 of 56 swapped answers got through. A swap with another chapter's answer was too easy (0 of 56 got through), so the eval swaps within a chapter: a near miss on the same topic.
- **What got through:** true statements from the lecture that don't answer the card's question ("Why is h(x) affine?" answered with a true fact about x₀). The judge checked whether the answer was true, not whether it answered the question.
- **Fix:** the judge's rules now say an answer must answer the question asked. Re-run: 86 of 86 kept, 30 of 30 wrong answers and 55 of 56 swaps removed. The one left is defensible (the swapped-in update rule contains the asked-for derivative).
- **Limits:** planted faults are easier to spot than the subtle mistakes a model makes, and the judge is also Claude. These numbers bound how lenient it is, not how often Opus is wrong.

## Hardening

- **A busy server says so:** with 5 lectures already queued, an upload is refused ("Engram is busy, try again in an hour") rather than left waiting for hours. Under 4 GB of free disk, uploads pause.
- **A course link before the course exists** goes to the job's progress page instead of a 404.
- **Empty states:** a lecture with no flashcards, no quiz questions, no diagrams or no recognised speech says so instead of showing an empty widget.
- **Retrying:** a failed job's page says to upload the same video again; finished steps are not repeated.

## Big files in Cloudflare R2, uploads straight from the browser

- **What:** uploads, web copies and summary videos live in an R2 bucket. The server's disk holds the lecture being processed (deleted after the job), frames, transcripts and the database.
- **Why:** this server also hosts DevSwarm and other sites. One CS229 run left 472 MB of source and 502 MB of web copy on disk; a few dozen uploads would fill it. R2 has no egress fees, which matters for video that's watched repeatedly.
- **The access code comes first:** before, the browser sent the whole file in one form post and the server checked the code afterwards, so anyone could push 2 GB at the server and have it discarded. Now the page sends the code with the file's name and size, and only a valid code gets a one-time upload slot. With R2, that slot is a signed PUT URL whose signature covers the key, the content type and the exact size, so it can't be reused for anything else or a bigger file. The browser uploads straight to R2: the server never handles the bytes.
- **Guessing is slow:** 10 wrong codes from one address in 10 minutes block that address for the rest of the window.
- **Without R2,** the same three steps run against this server (`PUT /uploads/<id>/file`, streamed, cut off at the declared size), so the form behaves the same way in both modes.
- **Alternatives:** a public bucket with unguessable names (anyone with a link could share the raw lecture forever; signed links expire after 6 hours); presigned POST policies (R2 doesn't support them; a signed PUT with the length in the signature does the same job); multipart uploads (only needed above 5 GB, and the limit is 2 GB).
- **Caching still works:** jobs uploaded to R2 use the object's ETag and size as their content id, so the same file uploaded twice still reuses the first job.

## Board-state recall: labelled by hand, scored strictly

- **What:** Devanshi marked, on contact sheets of one picture every 5 seconds, the moment each board was fullest just before it changed: 23 states in the first 20 minutes of CS229, 5 in MIT 18.06's first 6 minutes. A state counts as found if Engram kept a keyframe within 10 seconds of the mark, or a later keyframe whose view spans the mark (a board revisited or still on screen).
- **Result:** CS229 22 of 23, MIT 5 of 5. The miss, CS229 10:05, was a board still being written; the 10:32 keyframe shows it with more on it. It stays counted as missed: judging "same board, fuller" by eye is exactly the leniency the eval should avoid.
- **The first scorer was too generous:** it matched any keyframe after the previous mark, so a frame taken just after 0:25 "found" the board marked at 1:35, and it reported 100% with zero extra frames. Zero extras against 37 keyframes and 23 states didn't add up, which is how it was caught. The strict rule gives 96%.
- **The window's edge:** the last CS229 board (19:55) was kept at 21:22, after the 20-minute window. Later frames are now searched across the whole lecture.

## Handwriting accuracy dropped from the evals

- **What:** the plan had a character error rate against hand-typed boards. Devanshi started typing them and stopped: Ng's handwriting at 720p is often unreadable from the frame alone ("Supervisd lear" for "Supervised learning", words hidden behind him).
- **Why drop it rather than push on:** an answer key a person has to guess at measures the guess, not the reader. Engram itself never reads a frame bare: the words spoken at that moment go with it, and Opus re-opens unclear frames. The README says the number is missing and why.
- **Alternative:** label a lecture with clean slides or tablet writing. It would give a number, but for an easier case than the one Engram is built for.

## A labelling page that lost work

- **What happened:** the page saved marks a moment after each click, one write at a time. When a write finished before the next delayed save fired, that save found nothing new and wrote a record with no marks, which then cleared the page. A whole sheet of marks was lost.
- **Fix:** a save with nothing new is skipped, a record without labels never repaints the page, and a record older than the page's own last save is ignored. A simulated store with slow writes reproduced the bug in the old code and confirmed the fix before the page was used again.
- **Lesson:** debounced saves need a test of the "nothing left to save" path, not only of the busy path.

## M8: launch

### A daily limit per access code
- **What:** each code may start `ENGRAM_DAILY_UPLOADS` lectures (default 3) in any 24 hours. The check runs when the page asks for an upload slot, before any bytes move, and the refusal says when the code frees up.
- **Why:** every lecture spends ElevenLabs minutes, Haiku calls and Claude plan usage. One shared code shouldn't be able to drain them in an afternoon.
- **How it counts:** finished uploads (a job made, or an existing job reused) per hashed code, in a rolling window. Abandoned slots don't count. Re-uploading the same file still counts, though it costs nothing new: simpler to explain than an exception.
- **Alternatives:** a fixed reset at midnight (bursty, and whose midnight?); a global daily cap (one heavy user locks everyone out); per-IP limits (shared campus networks).

### HSTS in nginx, for this host only
- **What:** `Strict-Transport-Security: max-age=31536000`, set in the nginx site like the server's other HTTPS sites.
- **Why not `includeSubDomains` or `preload`:** both would commit every subdomain of devanshitiwari.com to HTTPS, and other projects live there. Preload is also hard to undo.

### A gallery of openly licensed lectures, credited
- **Picks:** MIT 18.06 lecture 1 (Strang, blackboard), MIT 6.0001 lecture 1 (Bell, slides), Khan Academy's "Reduced row echelon form 1" (Sal Khan, tablet). One for each kind of board Engram reads.
- **Licences checked at the source, not assumed:** both MIT videos say CC BY-NC-SA in their descriptions. Khan Academy's YouTube description doesn't. Their terms say content is for personal non-commercial use unless a page says otherwise, and this video's own page on khanacademy.org says "Creative Commons Attribution/Non-Commercial/Share-Alike". The terms also require the notice "All Khan Academy content is available for free at www.khanacademy.org", which its course page shows.
- **ShareAlike:** a generated course is an adaptation, so the course page says it's shared under the same licence.
- **Where the credit lives:** `credit.json` in the job folder, written by `engram list --source --url --license`. A credit without its licence is refused.

### How it works?, built from DevSwarm's walkthrough
- **What:** DevSwarm's animated relay (seven nodes on a rail, a token, one caption per step, dots that time the steps) with Engram's pipeline: lecture → listen → watch → read → write → check → course. The CSS is DevSwarm's; Engram adds only node placement and colours, and reuses DevSwarm's "locked" band as "every step saved" and its retry loop as "Opus opens the frame".
- **Why reuse it:** the brief is DevSwarm's look exactly, and its walkthrough already handles phones, reduced motion and pausing off screen.
- **The numbers in the captions are measured,** not illustrative: 41 frames opened and 20 corrected (CS229), 30 of 30 planted wrong answers caught, 22 of 23 board changes.

### A worker restart took over a job a shell was running
- **What happened:** the three gallery lectures ran with `engram run` in a shell. Mid-way through MIT 18.06, a deploy restarted the worker. On start-up the worker re-queues every job marked `running`, assuming a dead worker left it, so it began a second copy of the same job. Both rendered the summary video into the same temporary file, and one deleted it under the other.
- **Fix:** whoever runs a job holds an OS lock (`flock`) on `jobs/<id>/.lock`. A second runner backs off without changing anything; the worker re-queues only `running` jobs whose lock is free, and skips a queued job another process holds. The OS releases the lock when its process exits, however it exits, so a crash never leaves a job locked.
- **Alternatives:** a PID in the database (PIDs get reused, and it goes stale on a crash); a heartbeat column (needs a timeout, so a slow stage looks dead).
- **Lesson:** "running" in a database is a claim, not a fact. Check it against something the OS cleans up.

### Link previews
- **What:** Open Graph tags on every page. A course link shows that course's own thumbnail and overview; the rest of the site shows a cover image built from the three sample courses (`static/og.png`, rendered from HTML in the site's colours and font).
- **Why:** the launch is a LinkedIn post, and a pasted link without these shows a bare URL.
- **https behind nginx:** uvicorn trusts `X-Forwarded-Proto` from 127.0.0.1 by default, so `request.base_url` comes out as https without a hard-coded site URL.

### README GIFs
- Recorded with Playwright's video capture on the live site, converted with ffmpeg (12 fps, 960 px, a palette per GIF). Both use openly licensed lectures (Khan Academy), never CS229.

### After launch: HEAD requests and deleting jobs
- **HEAD:** FastAPI routes answer only the methods they declare, so every page returned 405 to HEAD, which uptime monitors and link checkers send. A small outer middleware serves HEAD as GET and drops the body, keeping the headers (including Content-Length). Starlette's own routes do this by default; FastAPI's don't.
- **`engram delete`:** a command rather than hand-written SQL, because a job lives in three places: its folder, its R2 objects and its database rows. The R2 keys come from the job's own `playback.json` and `summary.json`, and an upload another job also uses is never deleted.
