#!/usr/bin/env python3
"""Cut the three evaluation windows required by the annotation standard.

Rules: 2-5 min, ±1.5 min around midpoint, and last 4 min to last 1 min.
Boundary snapping is deliberately left to a human listener; these are exact cuts.
"""
from __future__ import annotations
import json
from pathlib import Path

def segments(duration: float) -> list[dict]:
    """Return three evaluation windows.

    The annotation standard uses three 3-minute windows. Any recording of at
    least 9 minutes must therefore have three windows; shorter recordings get
    three evenly-sized windows clipped to the recording instead of no windows.
    """
    if duration <= 0:
        return []
    if duration < 540:
        # Keep three windows for short test-drive clips. Roughly 20%-80% of
        # the clip is used, leaving margins for setup/teardown audio.
        usable=max(1.0, duration*0.6)
        length=usable/3
        start=max(0.0,(duration-usable)/2)
        return [{"name":n,"start":round(start+i*length,3),"end":round(start+(i+1)*length,3)}
                for i,n in enumerate(("front","middle","tail"))]
    length=180.0
    # For >=9 minutes, use the standard 3 x 3-minute layout.
    if duration < length*3:
        # 9..15 min: evenly distribute three windows across the clip.
        gap=(duration-length*3)/2
        return [{"name":"front","start":round(gap,3),"end":round(gap+length,3)},
                {"name":"middle","start":round(duration/2-length/2,3),"end":round(duration/2+length/2,3)},
                {"name":"tail","start":round(duration-gap-length,3),"end":round(duration-gap,3)}]
    return [
        {"name": "front", "start": 120.0, "end": 300.0},
        {"name": "middle", "start": round(duration / 2 - 90, 3), "end": round(duration / 2 + 90, 3)},
        {"name": "tail", "start": round(duration - 240, 3), "end": round(duration - 60, 3)},
    ]

if __name__ == "__main__":
    import sys
    d = float(sys.argv[1])
    print(json.dumps(segments(d), ensure_ascii=False, indent=2))
