"""Compatibility imports for shared nonblocking byte-endpoint validation."""

from agentworks.execution._process import SinkWriteError, try_write_to_sink

__all__ = ["SinkWriteError", "try_write_to_sink"]
