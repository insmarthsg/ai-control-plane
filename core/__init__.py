"""Core control-plane primitives (harness-neutral).

Nothing in this package may import an execution adapter, decode an execution
log, or depend on a harness's error-code vocabulary. Core consumes plain data
(HTTP status, headers, adapter-supplied hints) and returns plain data.
"""
