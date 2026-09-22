"""Detect recurring tool results without limiting useful research depth."""
from collections import OrderedDict
import hashlib
import json


class ToolProgress:
    def __init__(self, repeat_limit=4):
        self.repeat_limit = repeat_limit
        self.seen = OrderedDict()
        self.reading = None
        self.stalled = 0

    def repeated(self, batch, read_cache):
        reading = tuple(sorted((key, tuple(tuple(span) for span in value.get("read_ranges", [])),
                                value.get("body_hash"), value.get("text", ""))
                               for key, value in read_cache.items()))
        if reading != self.reading:
            self.seen.clear()
            self.stalled = 0
            self.reading = reading
        signature = hashlib.sha256(json.dumps(batch, ensure_ascii=False, sort_keys=True).encode()).digest()
        if signature in self.seen:
            self.stalled += 1
            self.seen.move_to_end(signature)
        else:
            self.stalled = 0
            self.seen[signature] = None
            if len(self.seen) > 64:
                self.seen.popitem(last=False)
        return self.stalled >= self.repeat_limit - 1
