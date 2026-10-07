"""Private execution protection choices and their future guarantees."""

from enum import Enum


class Protection(Enum):
    """Requested process protection, with no implicit downgrade."""

    DIRECT = "direct"
    MANAGED = "managed"
