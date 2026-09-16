"""Isolated browser execution.

The runtime never drives a browser itself. It dispatches a named, typed,
reviewed operation to a separate worker process and records what comes back.
"""
