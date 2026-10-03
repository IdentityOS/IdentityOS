"""Durable secrets storage for IdentityOS operators.

Secrets live outside the identity store, in a mode-0700 directory with mode-0600
files, and are referenced by opaque handles (``secret://<handle>``) everywhere
else.  Capabilities may resolve a handle at execution time; plaintext values are
never written to identity state, provenance, messages, or logs.
"""