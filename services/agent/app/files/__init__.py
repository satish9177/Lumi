"""Milestone 10: bounded local file authority (approved M10 roots, dropped files, placed transfers).

Nothing in this package accepts an absolute path from a model, a provider or the renderer. Absolute
paths enter only from Electron main (a native folder dialog, or its own dropped-file store) and never
leave the runtime on any route, event or log line.
"""
