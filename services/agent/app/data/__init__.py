"""Pinned data files that ship with the runtime.

Nothing here is downloaded, generated or refreshed at run time. Each file is a
snapshot taken deliberately at development time, committed, digested, and
checked against that digest when it is loaded -- so a corrupted, truncated or
substituted copy fails the load rather than quietly changing a security
decision.
"""
