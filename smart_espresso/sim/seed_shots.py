"""A short saved-shot log for screenshots and tests.

The live demo only commits one shot per minute, so a fresh sim cannot
show four tiles and an All 5 shots link in one frame. This writes five
shots, the fourth newest dated yesterday.
"""

import os
from datetime import datetime, timedelta

from smart_espresso.json_store import atomic_write_json

DEMO_MLS = (22.0, 34.0, 34.0, 41.0, 39.0)
DEMO_SECONDS = (18.0, 28.0, 26.0, 33.0, 27.0)


def write_demo_log(path, now=None):
    """Write five shots. ``now`` is the newest moment; the oldest is yesterday."""
    moment = now or datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    stamps = (
        moment - timedelta(days=2, hours=3),
        moment - timedelta(days=1, hours=2),
        moment - timedelta(hours=5),
        moment - timedelta(hours=3),
        moment - timedelta(hours=1),
    )
    shots = []
    for index, stamp in enumerate(stamps):
        shots.append(
            {
                "id": index + 1,
                "started_at": stamp.isoformat(timespec="seconds"),
                "seconds": DEMO_SECONDS[index],
                "ml": DEMO_MLS[index],
                "peak_head_bar": 9.0,
                "peak_boiler_bar": 1.2,
                "peak_flow_mls": 2.0,
                "samples": [
                    {
                        "t": 0.0,
                        "head_bar": 9.0,
                        "boiler_bar": 1.2,
                        "flow_mls": 1.5,
                    }
                ],
                "clock_synced": True,
            }
        )
    atomic_write_json(
        os.path.expanduser(path),
        {"version": 1, "next_id": len(shots) + 1, "shots": shots},
    )
    return shots
