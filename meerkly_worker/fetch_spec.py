"""The fetch-job wire contract.

Canonical source: api-gateway/spec/fetch-job.schema.json and its vectors. This
module is conformance-tested against them, so changing anything here means
changing the spec first.

parse_fetch_frame returns a plain dict with wire-shaped camelCase keys, so the
conformance test is a direct == against the vector.
"""

from __future__ import annotations

import math
from typing import Any

WAIT_FOR_DEFAULT = "stable"
SETTLE_DEFAULT_MS = 5000
SETTLE_MAX_MS = 25000
DETECT_DEFAULT_MS = 200
DETECT_MAX_MS = 25000
STABLE_QUIET_MS = 500


def _positive_int(value: Any) -> int:
    # bool subclasses int in Python, so exclude it: JSON `true` is not 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return math.floor(value) if value > 0 else 0


def parse_fetch_frame(msg: Any) -> dict | None:
    """Parse a gateway `fetch` frame; None when it is unusable.

    A None return means no result is sent at all and the job times out
    gateway-side, which is the defined behavior for a malformed frame.
    """
    if not isinstance(msg, dict):
        return None

    job_id, url = msg.get("jobId"), msg.get("url")
    if not isinstance(job_id, str) or not job_id:
        return None
    if not isinstance(url, str) or not url:
        return None

    wait_for = msg.get("waitFor")
    wait_for = wait_for if isinstance(wait_for, str) and wait_for else WAIT_FOR_DEFAULT

    rules: list[dict] = []
    raw_rules = msg.get("waitRules")
    if isinstance(raw_rules, list):
        for rule in raw_rules:
            if not isinstance(rule, dict):
                continue
            guard = rule.get("if")
            if not isinstance(guard, str) or not guard:
                continue
            target = rule.get("then")
            rules.append({"if": guard, "then": target if isinstance(target, str) else ""})

    return {
        "jobId": job_id,
        "url": url,
        "waitFor": wait_for,
        # Not clamped here: the cap also depends on the remaining navigation
        # budget, which only the browser knows.
        "settleMs": _positive_int(msg.get("settleMs")),
        "waitRules": rules,
        "detectMs": _positive_int(msg.get("detectMs")),
    }


def effective_settle_cap(settle_ms: int, budget_ms: float = math.inf) -> int:
    return int(min(settle_ms if settle_ms > 0 else SETTLE_DEFAULT_MS, budget_ms, SETTLE_MAX_MS))


def effective_detect_probe(detect_ms: int, budget_ms: float = math.inf) -> int:
    return int(min(detect_ms if detect_ms > 0 else DETECT_DEFAULT_MS, budget_ms, DETECT_MAX_MS))
