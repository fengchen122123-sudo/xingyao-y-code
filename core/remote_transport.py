from __future__ import annotations

import json
from collections import deque


class EventReplayBuffer:
    def __init__(self, maxlen: int = 128):
        self.seq = 0
        self.items: deque = deque(maxlen=maxlen)

    def stamp(self, payload: str) -> str:
        try:
            parsed = json.loads(payload)
        except Exception:
            return payload
        if not isinstance(parsed, dict) or "type" not in parsed:
            return payload
        self.seq += 1
        parsed["seq"] = self.seq
        try:
            stamped = json.dumps(parsed, ensure_ascii=False)
        except Exception:
            self.seq -= 1
            return payload
        self.items.append((self.seq, stamped))
        return stamped
