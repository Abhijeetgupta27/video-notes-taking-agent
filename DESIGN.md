# Design notes

Things the spec left open (section 10) and why I went the way I did.

## Broker: RabbitMQ (quorum queue)

- Manual acks + "unacked messages go back to the queue when the consumer's connection dies" is exactly the crash-recovery behaviour the project asks for, without writing it myself.
- Quorum queue instead of a classic one: replicated/durable by design and it counts redeliveries (`x-delivery-count`), which I use to catch jobs that keep crashing workers.
- Delayed retries without plugins: one small queue per delay (`notes.retry.10s` ...) with a message TTL and a dead-letter route back to `notes.jobs`.
- Publisher confirms on every publish, so "next stage is queued" is actually true before I ack the current one.

Redis Streams would also work (XCLAIM for stuck messages), but I'd have to write the pending-message reclaiming and the delayed retry logic myself.

## Job state: Redis

- `job:<id>` hash = what the API returns (status, progress, warnings, error, result, detail, waiting).
- `job:<id>:a:<name>` = stage artifacts (transcript, each LLM chunk result, notes) with a 24h TTL.
- `job:<id>:frames` + `job:<id>:img:<ts>` = per-timestamp frame status and the JPEG bytes. Storing frames in Redis means any worker container can publish them, no shared volume needed.
- `job:<id>:done` / `:fwd` sets = which stages finished / were forwarded. These two sets are what make every step of the consumer safe to replay.
- AOF is on (`--appendonly yes`) so state survives a Redis restart.

State machine lives in `common/states.py`. Every transition goes through `store.move()` which uses WATCH/MULTI so two writers can't race, rejects backwards moves and treats "same state again" as a no-op.

## Worker topology: one worker type, one message per stage

Every worker can run every stage, but each stage is its own queue message (`{job_id, stage}`). After a stage finishes:

```
checkpoint done → publish next stage (confirmed) → mark forwarded → ack
```

- if the worker dies before the ack, the stage is redelivered; it sees "done" and only forwards (if that didn't happen yet)
- so a kill can at most cost the one stage that was running, and even that resumes from its own checkpoints
- different stages of different jobs interleave naturally across the pool; adding workers is just `--scale worker=N`

I considered specialised workers per stage (separate frames workers etc.) but with 4 short stages it's more containers to explain for no real gain - the frames stage isn't heavy enough to need its own pool.

Per-job lock (`lock:<id>`) + worker heartbeat (`worker:<id>`, 6s TTL): a message can only be processed by the worker that holds the job's lock. If the holder is dead (no heartbeat), the lock is taken over immediately - that's what bounds recovery time after a SIGKILL. This guards against the rare case where RabbitMQ redelivers while the first worker is actually still alive (e.g. network blip).

## Global LLM rate limit

Sliding-window log in Redis, done in one Lua script so it's atomic across all workers:

- two sorted sets per limiter: request timestamps, and `cost:id` entries for tokens
- a call is allowed only if both the request count and the token sum in the last window stay under the limit; otherwise the script returns how long to wait
- token cost is estimated before the call (prompt chars / 3 + max_tokens) and reserved. after the call the
  reservation is swapped for the real `usage.total_tokens` Groq returns ("reserve then settle"), so unused
  tokens go straight back to the shared budget instead of blocking other workers for a minute
- the LLM window is 63s instead of 60s: in testing, calls released right at the 60s edge could arrive at the provider slightly *inside* its window, the extra 3s removes that

I picked a sliding window over a token bucket because a bucket allows a burst of `capacity` right at the start of a window plus refill, so in the worst case ~2x the limit in 60s - and the requirement is that a burst never causes a 429. The same limiter is used for Notion (3 req/s per token) and YouTube.

## LLM

Groq (`openai/gpt-oss-120b`, with `reasoning_effort=low`) through the plain OpenAI-compatible API with `httpx`, so changing provider is just env vars. JSON mode on.

Long transcripts: caption snippets are merged into ~12s lines (`[seconds] text`), split into chunks of `CHUNK_CHARS`, and every chunk is summarised separately into `sections` + `visuals`. Results are concatenated in chunk order (section start times are clamped into their chunk's span so the order can't get mixed up). One last small call writes the overview + key takeaways from the section notes.

Visual timestamps are deduplicated (two flags within 8s = same slide, keep the more important one), clamped to the video length, and capped at `MAX_FRAMES`, keeping the highest "importance".

## Transcript & frames

- Metadata (title, duration) via `yt-dlp` without downloading. That's also where constraint A is checked.
- Transcript via `youtube-transcript-api`; if YouTube blocks that, fall back to the caption track yt-dlp sees (`json3` format).
- Frames: `yt-dlp` gives a direct ≤720p stream URL, `ffmpeg -ss <t> -i <url> -frames:v 1` seeks with HTTP range requests and decodes one frame - no full download. In practice YouTube often answers 403 to plain ffmpeg reads of that URL, so on the first failure the worker falls back to letting yt-dlp download a ≤480p video-only copy (its own downloader handles YouTube's quirks) into a temp dir and cuts all remaining frames from that file. Still real frames from the same video, just read from disk.

## Notion publishing

1. upload each JPEG with the File Upload API (ids remembered in Redis)
2. create the page with only its first block (a callout containing the job id)
3. append the rest in batches of 90, starting at `children_count(page)` - Notion itself is the source of truth for how far we got

## Extension

Plain Manifest V3, no framework and no build step.

- `content.js` - adds the button to the player's right controls. YouTube is a single-page app, so it listens for `yt-navigate-finish` and also runs a throttled MutationObserver; the video id is read at click time, never cached.
- `background.js` - OAuth via `chrome.identity.launchWebAuthFlow`; the code is exchanged through the gateway (client secret never ships in the extension). Jobs and the token live in `chrome.storage` because MV3 service workers get suspended. Polls every 2s while something is running, with a `chrome.alarms` backup.
- `popup` - one card per job with the step sequence, progress bar, retry countdown, warnings/errors in plain words and a "View in Notion" button.
