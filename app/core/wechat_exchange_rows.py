"""Share exact bounded SQLite cell encoding across WeChat exchange operations.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import base64
import json
import math


MAX_ROW_BYTES = 16 * 1024**2


def typed_cell(value):
    """Represent native SQLite values without lossy JSON numeric conversions."""
    if value is None:
        return ["null", None]
    if type(value) is int and -(2**63) <= value < 2**63:
        return ["integer", str(value)]
    if type(value) is float and math.isfinite(value):
        return ["real", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, bytes):
        return ["blob", base64.b64encode(value).decode("ascii")]
    raise ValueError("A preserved SQLite cell has an unsupported value.")


def encode_typed_row(row, columns):
    """Apply the same canonical typed-row byte budget before export or import."""
    cells = [[column, *typed_cell(row[column])] for column in columns]
    value = json.dumps(cells, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False).encode()
    if len(value) > MAX_ROW_BYTES:
        raise ValueError("A preserved exchange row exceeds its bounded size.")
    return value
