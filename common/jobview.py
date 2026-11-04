# turns the raw redis hash into what the API returns
import json


def _j(v, default=None):
    if not v:
        return default
    try:
        return json.loads(v)
    except ValueError:
        return default


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def job_view(h):
    return {
        "job_id": h.get("job_id"),
        "status": h.get("status"),
        "progress": _num(h.get("progress")) or 0,
        "video_id": h.get("video_id"),
        "video_title": h.get("video_title") or None,
        "duration_sec": _num(h.get("duration_sec")),
        "warnings": _j(h.get("warnings"), []),
        "error": _j(h.get("error")),
        "result": _j(h.get("result")),
        # extras (not in the spec, used by the popup + demo scripts)
        "detail": h.get("detail") or None,
        "waiting": _j(h.get("waiting")),
        "worker": h.get("worker") or None,
        "created_at": h.get("created_at"),
        "updated_at": h.get("updated_at"),
    }
