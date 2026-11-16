"""Named monitoring plans; public phase identifiers and scene contracts stay unchanged."""

from copy import deepcopy

PHASES = [
    ("SIGNIN", "入场签到"),
    ("DISCIPLINE", "宣读纪律"),
    ("PRELIM", "初步评审"),
    ("DETAIL", "详细评审"),
    ("CLARIFY", "澄清讨论"),
    ("REPORT", "收尾宣读"),
]
DEFAULT_PLANS = {
    "SIGNIN": ["S01", "S04", "S10"],
    "DISCIPLINE": ["S01", "S06", "S10"],
    "PRELIM": ["S02", "S03", "S04", "S05", "S08", "S10"],
    "DETAIL": ["S02", "S03", "S04", "S05", "S08", "S10"],
    "CLARIFY": ["S03", "S04", "S06", "S07", "S08", "S10"],
    "REPORT": ["S03", "S04", "S06", "S10"],
}
BASIC_PLANS = {
    phase: ["S01", "S02", "S08", "S10"]
    if phase in ("SIGNIN", "DISCIPLINE")
    else ["S02", "S05", "S08", "S10"]
    if phase in ("PRELIM", "DETAIL")
    else ["S02", "S08", "S10"]
    for phase, _ in PHASES
}
PROFILE_LABELS = {"basic": "基础行为监管", "standard": "完整现场监管"}


def profile_plans(key):
    return deepcopy(BASIC_PLANS if key == "basic" else DEFAULT_PLANS)
