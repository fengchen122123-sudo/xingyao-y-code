import json
from copy import deepcopy


ALPHA_TOOL = {
    "type": "function",
    "function": {
        "name": "alpha",
        "description": "alpha",
        "parameters": {"type": "object", "properties": {}},
    },
}

ZETA_TOOL = {
    "type": "function",
    "function": {
        "name": "zeta",
        "description": "zeta",
        "parameters": {"type": "object", "properties": {}},
    },
}


def snapshot_provider_kwargs(kwargs: dict) -> bytes:
    safe = deepcopy(kwargs)
    if "api_key" in safe:
        safe["api_key"] = "<redacted>"
    return json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
