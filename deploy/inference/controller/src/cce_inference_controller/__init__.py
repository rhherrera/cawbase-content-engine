"""A09-1 inference controller: starts the GPU host on durable demand, waits for real model readiness, gates
dispatch, drains and stops it after continuous idleness. Runs outside the GPU host and outside the engine package.

Importing this package has no side effects (no connections, threads, timers or environment reads).
"""

__version__ = "0.1.0"
