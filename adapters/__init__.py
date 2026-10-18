"""Execution-harness adapters.

Everything here maps an external harness surface onto core primitives. The
current DSH adapter is FALLBACK_ONLY and marked TEMPORARY_INTERNAL_INTERFACE:
it reads ``session.v4.jsonl.zstd`` because DSH exposes no external error API.
"""
