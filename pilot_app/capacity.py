"""How many pilot accounts this machine can actually carry.

Why this is not just a number
-----------------------------
The honest answer has a reason attached, and the reason changes as the load
changes. Round 20 measured this project's shape: the binding constraint is
*model latency × concurrency slots*, not CPU or RAM — a report takes seconds of
provider time, and only ``INFE_PILOT_REPORT_WORKERS`` of them can be in flight.
So the advisor computes several ceilings, takes the smallest, and says which one
bound it. An operator who disagrees can see the arithmetic and override it.

Everything here is a pure function of numbers that were measured elsewhere, so
every threshold and every failure mode can be tested without a real server.
"""

from __future__ import annotations

import statistics
from typing import Any

# Used only until the install has produced enough runs to measure its own
# numbers. Deliberately pessimistic: assuming a slow report is the safe
# direction, because over-estimating capacity is what makes a pilot feel broken.
DEFAULT_REPORT_SECONDS = 60.0
# A typical student's CityU traffic: a handful of notices a day. Also a guess,
# and also rounded up, for the same reason.
DEFAULT_MAILS_PER_USER_DAY = 3.0
# Samples needed before the measured generation time is trusted. With two users
# a fresh install has almost no data, and a median of two numbers is not a
# measurement.
CONFIDENT_SAMPLES = 10
REASONABLE_SAMPLES = 3

# Never recommend filling the machine to the brim: the point of a headroom
# factor is that the bad day is the one that matters, not the average day.
SAFETY_FACTOR = 0.7
# Above these, the machine itself becomes the constraint and the recommendation
# stops growing until it comes back down.
BUSY_CPU_PERCENT = 70.0
BUSY_MEMORY_PERCENT = 80.0
BUSY_DISK_PERCENT = 80.0

# A policy ceiling, not a measurement, and it is the constraint that usually
# binds. Round 20 sized this design at a comfortable 50-75 users with a hard
# ceiling near 220 on one 2 vCPU box, with model latency x concurrency slots as
# the limit.
#
# The arithmetic on its own happily produces tens of thousands, which is worse
# than useless: nobody acts on a number like that, and printing it once is enough
# to lose the operator's trust in every other number on the panel. Two unknowns
# also sit beyond it and neither is ours to guess — QQ publishes no IMAP request
# limit, and one server IP opening thousands of mailboxes a day has never been
# validated.
PILOT_COMFORTABLE_USERS = 75
PILOT_HARD_CEILING = 220


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def advise(*, volume: dict[str, Any], host: dict[str, Any], workers: int,
           current: int, source: str) -> dict[str, Any]:
    """Return the current cap plus a reasoned suggestion for the next one.

    ``volume`` comes from :meth:`Database.recent_volume`, ``host`` from
    :func:`metrics.host_metrics`. Both are passed in so this stays pure.
    """
    window_days = max(1, int(volume.get("window_days") or 1))
    total_users = max(0, int(volume.get("total_users") or 0))
    messages = max(0, int(volume.get("messages") or 0))
    active_users = max(0, int(volume.get("active_users") or 0))
    gaps = [float(gap) for gap in (volume.get("generation_gaps") or []) if gap]

    measured = len(gaps) >= REASONABLE_SAMPLES
    report_seconds = statistics.median(gaps) if measured else DEFAULT_REPORT_SECONDS
    if active_users:
        mails_per_user_day = max(0.05, messages / active_users / window_days)
    else:
        mails_per_user_day = DEFAULT_MAILS_PER_USER_DAY

    # -- constraint 1: how many reports this install can produce in a day -----
    per_slot_per_day = 86400.0 / max(1.0, report_seconds)
    reports_per_day = max(1.0, float(max(1, workers)) * per_slot_per_day)
    by_generation = reports_per_day / mails_per_user_day

    constraints: list[dict[str, Any]] = [{
        "name": "generation",
        "limit": by_generation,
        "reason": (
            f"按「同用户相邻报告间隔」约 {report_seconds:.0f} 秒（这是上界，不是实测生成耗时）"
            f"与 {workers} 个并发槽位估算，每天约 {reports_per_day:.0f} 份；"
            f"再按每人每天 {mails_per_user_day:.1f} 封算"
        ),
    }]

    # -- constraints 2..4: the machine itself ---------------------------------
    # These only bind when the machine is already busy. Scaling linearly from an
    # idle baseline would produce a meaningless "infinity users", and inventing a
    # per-user cost that has never been observed would be worse than saying
    # nothing.
    cpu = _number((host.get("cpu_percent")))
    memory = _number(((host.get("memory") or {}).get("percent")))
    disk = _number(((host.get("disk") or {}).get("percent")))

    if cpu is not None and cpu >= BUSY_CPU_PERCENT:
        constraints.append({"name": "cpu", "limit": float(max(0, total_users)),
                            "reason": f"CPU 已在 {cpu:.0f}%，先不要再加人"})
    if memory is not None and memory >= BUSY_MEMORY_PERCENT:
        constraints.append({"name": "memory", "limit": float(max(0, total_users)),
                            "reason": f"内存已用 {memory:.0f}%，先不要再加人"})
    if disk is not None and disk >= BUSY_DISK_PERCENT:
        constraints.append({"name": "disk", "limit": float(max(0, total_users)),
                            "reason": f"磁盘已用 {disk:.0f}%，先清理再扩"})

    # The single-box policy ceiling. It is listed last only so the machine
    # constraints above can take priority when they are the real problem.
    constraints.append({
        "name": "single_box",
        "limit": float(PILOT_COMFORTABLE_USERS),
        "reason": (f"单机试点的舒适上限是 {PILOT_COMFORTABLE_USERS} 人"
                   f"（第 20 轮容量评估的硬上限约 {PILOT_HARD_CEILING} 人）；"
                   "再往上，供应商对单 IP 的 IMAP 频率没有公开数字，我们没有验证过"),
    })

    binding = min(constraints, key=lambda item: item["limit"])
    raw = binding["limit"] * SAFETY_FACTOR
    # Round down to a round-ish number; an operator reading "23" trusts it less
    # than "20" and the difference does not matter.
    step = 5 if raw >= 20 else 1
    recommended = int(raw // step * step)
    # Never suggest going backwards as a "recommendation" — shrinking the pilot
    # is a decision, not advice.
    recommended = max(1, recommended)
    if recommended < current:
        recommended = current

    notes: list[str] = []
    if not measured:
        notes.append(
            f"报告间隔还在用保守默认值 {DEFAULT_REPORT_SECONDS:.0f} 秒"
            f"（只采到 {len(gaps)} 个样本，需要 {REASONABLE_SAMPLES} 个以上才按实测算）。"
        )
    if not active_users:
        notes.append(f"最近 {window_days} 天没有来信，每人每天 {DEFAULT_MAILS_PER_USER_DAY:.0f} 封是默认假设。")
    if binding["name"] == "generation":
        notes.append("当前瓶颈是模型生成速度，不是服务器——加机器没用，换更快的模型才有用。")
    elif binding["name"] == "single_box" and cpu is not None and memory is not None:
        # The operator asked what the live pressure says, so answer it directly
        # rather than leaving them to infer it from the ceilings.
        notes.append(
            f"服务器目前很轻松（CPU {cpu:.0f}%、内存 {memory:.0f}%），"
            "所以限制名额的不是机器，而是「一台机器 + 一个人维护」这个试点设定。"
        )

    # -- what the traffic is doing right now ----------------------------------
    load: list[str] = []
    if cpu is not None:
        load.append(f"CPU {cpu:.0f}%")
    if memory is not None:
        load.append(f"内存 {memory:.0f}%")
    if disk is not None:
        load.append(f"磁盘 {disk:.0f}%")
    if active_users:
        load.append(f"最近 {window_days} 天每人每天 {mails_per_user_day:.1f} 封")

    return {
        "current": int(current),
        "source": source,
        "recommended": recommended,
        "binding": binding["name"],
        "binding_reason": binding["reason"],
        "constraints": constraints,
        "confidence": ("high" if len(gaps) >= CONFIDENT_SAMPLES
                       else "medium" if measured else "low"),
        "measured": {
            "report_seconds": round(report_seconds, 1),
            "report_seconds_from_data": measured,
            "samples": len(gaps),
            "mails_per_user_day": round(mails_per_user_day, 2),
            "active_users": active_users,
            "total_users": total_users,
            "window_days": window_days,
            "workers": int(max(1, workers)),
            "reports_per_day": round(reports_per_day, 1),
            "cpu_percent": cpu,
            "memory_percent": memory,
            "disk_percent": disk,
        },
        "load": load,
        "notes": notes,
    }
