"""Core control-plane primitives (harness-neutral).

Nothing in this package may import ``adapters``, DSH paths, zstd, or session
log schemas. The core consumes plain data (HTTP status, headers, DSH error
codes) and returns plain data.
"""
