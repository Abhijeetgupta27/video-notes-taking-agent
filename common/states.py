QUEUED = "QUEUED"
TRANSCRIBING = "TRANSCRIBING"
SUMMARIZING = "SUMMARIZING"
EXTRACTING_FRAMES = "EXTRACTING_FRAMES"
PUBLISHING = "PUBLISHING"
COMPLETED = "COMPLETED"
COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
FAILED = "FAILED"
DEAD_LETTERED = "DEAD_LETTERED"

TERMINAL = {COMPLETED, COMPLETED_WITH_WARNINGS, FAILED, DEAD_LETTERED}

# allowed forward moves. FAILED / DEAD_LETTERED are reachable from any non terminal state
NEXT = {
    QUEUED: {TRANSCRIBING},
    TRANSCRIBING: {SUMMARIZING},
    SUMMARIZING: {EXTRACTING_FRAMES},
    EXTRACTING_FRAMES: {PUBLISHING},
    PUBLISHING: {COMPLETED, COMPLETED_WITH_WARNINGS},
}

# stage name in the queue message -> status while it runs
STAGES = ["transcribe", "summarize", "frames", "publish"]
STAGE_STATUS = {
    "transcribe": TRANSCRIBING,
    "summarize": SUMMARIZING,
    "frames": EXTRACTING_FRAMES,
    "publish": PUBLISHING,
}


def can_move(cur, new):
    if cur in TERMINAL:
        return False
    if new in (FAILED, DEAD_LETTERED):
        return True
    return new in NEXT.get(cur, set())


def next_stage(stage):
    i = STAGES.index(stage)
    return STAGES[i + 1] if i + 1 < len(STAGES) else None
