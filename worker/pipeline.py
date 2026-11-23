# the four stages. each one is safe to run again for the same job (redelivery after a crash):
# finished work is checkpointed in redis and skipped on the second run.
import logging
import shutil
import tempfile
import time

from common import config
from common import states as S
from common.secret import unseal

from . import llm, notion, store
from . import youtube as yt
from .errors import Fatal, RateLimited, Retryable, StageError

log = logging.getLogger("worker.pipeline")


def run(stage, jid, msg):
    fn = {"transcribe": transcribe, "summarize": summarize, "frames": frames, "publish": publish}[stage]
    fn(jid, store.get(jid), msg)


def transcribe(jid, job, msg):
    vid = job["video_id"]
    store.move(jid, S.TRANSCRIBING, progress=5, detail="Looking up the video", waiting="")
    m = yt.meta(vid)
    if m["is_live"]:
        raise Fatal("LIVE_VIDEO", "Live streams can't be processed")
    dur = m["duration"]
    if not dur:
        # yt-dlp sometimes returns an empty result instead of an error for private / deleted videos.
        # treat it as unavailable (retryable) so it ends up in the DLQ like other unavailable videos
        raise Retryable("VIDEO_UNAVAILABLE", "YouTube returned no details for this video - "
                                             "it may be private, deleted or unavailable")
    store.update(jid, video_title=m["title"], duration_sec=int(dur))

    # constraint A - reject before doing any real work
    if dur > config.MAX_VIDEO_SEC:
        raise Fatal("VIDEO_TOO_LONG", f"Video is {llm.mmss(dur)} long - only videos up to "
                                      f"{llm.mmss(config.MAX_VIDEO_SEC)} are supported")

    store.update(jid, progress=10, detail="Downloading the transcript")
    lines, note = yt.transcript(vid, m)
    if not lines:
        raise Fatal("NO_TRANSCRIPT", "Transcript is empty")
    if note:
        store.add_warning(jid, {"code": "TRANSCRIPT_NOT_ENGLISH", "message": note})
    store.put(jid, "transcript", lines)
    store.update(jid, progress=20, detail=f"Transcript ready ({len(lines)} caption lines)")


def summarize(jid, job, msg):
    store.move(jid, S.SUMMARIZING, progress=25, detail="Preparing transcript for the LLM", waiting="")
    title = job.get("video_title") or job["video_id"]
    chunks = llm.chunk(llm.to_lines(store.load(jid, "transcript")))
    parts = []
    for i, c in enumerate(chunks):
        p = store.load(jid, f"chunk{i}")
        if p is None:
            store.update(jid, detail=f"Summarizing part {i + 1} of {len(chunks)}")
            p = llm.summarize_chunk(jid, title, c, i, len(chunks))
            store.put(jid, f"chunk{i}", p)
        parts.append(p)
        store.update(jid, progress=25 + int(25 * (i + 1) / len(chunks)))

    notes = llm.merge(parts, int(job.get("duration_sec") or 0))
    ov = store.load(jid, "overview")
    if ov is None:
        store.update(jid, detail="Writing the overview")
        ov = llm.overview(jid, title, notes["sections"])
        store.put(jid, "overview", ov)
    notes.update(ov)
    store.put(jid, "notes", notes)
    store.update(jid, progress=55, detail=f"Notes ready, {len(notes['visuals'])} visual moments flagged")


def frames(jid, job, msg):
    store.move(jid, S.EXTRACTING_FRAMES, progress=60, detail="Opening the video stream", waiting="")
    vis = store.load(jid, "notes")["visuals"]
    done = store.frame_states(jid)
    todo = [v for v in vis if str(v["timestamp_sec"]) not in done]
    total = len(vis)
    if not todo:
        store.update(jid, progress=85, detail=f"{total} frame(s) already handled")
        return

    def fail(ts, why):
        store.frame_fail(jid, ts, why)
        store.add_warning(jid, {"code": "FRAME_EXTRACTION_FAILED", "timestamp_sec": ts, "message": why})

    try:
        url, hdrs = yt.stream_url(job["video_id"])
    except RateLimited:
        raise
    except StageError as e:
        # can't open the stream at all -> every frame is omitted, page goes out text only
        for v in todo:
            fail(v["timestamp_sec"], e.msg)
        return

    # "stream" = ffmpeg reads the youtube url directly (fast, no download)
    # "file"   = youtube refused that (403 is common now), so we pulled a small copy with yt-dlp
    mode = "stream"
    tmp = tempfile.mkdtemp(prefix=f"frames-{jid[:8]}-")
    try:
        for v in todo:
            ts = v["timestamp_sec"]
            n_done = len(store.frame_states(jid))
            store.update(jid, progress=60 + int(25 * n_done / total),
                         detail=f"Capturing frame {n_done + 1} of {total} at {llm.mmss(ts)}")
            if config.FRAME_DELAY:
                time.sleep(config.FRAME_DELAY)
            try:
                img = yt.grab_frame(url, hdrs, ts)
            except yt.FrameError as e:
                if mode != "stream":
                    fail(ts, str(e))
                    continue
                log.info("job %s: direct stream read failed (%s), downloading a low-res copy", jid, e)
                store.update(jid, detail="Stream refused, downloading a low-res copy of the video")
                try:
                    url, hdrs = yt.download_low(job["video_id"], tmp), {}
                    mode = "file"
                    img = yt.grab_frame(url, hdrs, ts)
                except RateLimited:
                    raise
                except (yt.FrameError, StageError) as e2:
                    mode = "broken"  # don't try the download again for every timestamp
                    fail(ts, str(e2))
                    continue
            store.frame_ok(jid, ts, img)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    store.update(jid, progress=85, detail="Frames captured")


def publish(jid, job, msg):
    store.move(jid, S.PUBLISHING, progress=88, detail="Publishing to Notion", waiting="")
    notes = store.load(jid, "notes")
    fs = store.frame_states(jid)
    ok = []
    for v in notes["visuals"]:
        if fs.get(str(v["timestamp_sec"])) == "ok":
            img = store.frame_img(jid, v["timestamp_sec"])
            if img:
                ok.append((v, img))

    url, published = notion.publish(jid, job, notes, ok, unseal(msg["token"]), msg.get("parent_id"),
                                     progress=lambda t: store.update(jid, detail=t))
    ws = store.warnings(jid)
    status = S.COMPLETED_WITH_WARNINGS if ws or published < len(notes["visuals"]) else S.COMPLETED
    result = {"notion_page_url": url, "frames_requested": len(notes["visuals"]), "frames_published": published}
    store.move(jid, status, progress=100, result=result, waiting="",
               detail="Done" if status == S.COMPLETED else "Done, with warnings")
    log.info("job %s -> %s %s", jid, status, url)
