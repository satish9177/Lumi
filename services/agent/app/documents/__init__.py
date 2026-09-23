"""Milestone 10 S1: bounded document text extraction.

Everything here turns *bytes* into bounded plain text. No module in this package opens a path, fetches a
URL, resolves an external relationship, runs a macro, evaluates PDF JavaScript, follows an action or
opens another application. Extraction runs in a separate helper process (`app.documents.helper`) that
receives the bytes on stdin; the runtime reads the file itself, from a handle it has verified.

Extracted text is private local data and `untrusted_environment`: extraction never makes it trusted.
"""
