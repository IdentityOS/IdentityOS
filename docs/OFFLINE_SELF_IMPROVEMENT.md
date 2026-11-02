# Offline self-improvement

IdentityOS can author, independently test, publish, install, persist, and reuse
a capability without a network call. Authoring alone does not change the live
workspace. Publication requires an external, one-use push invitation bound to
the exact identity, capability ID, and tested artifact digest.

## Lifecycle

```text
need -> independent cases -> author -> isolated verification -> staged artifact
     -> BLOCKED (push invitation required)
external controller issues exact-artifact invitation
     -> atomic workspace publication -> install-time verification -> install
     -> fresh-registry reload -> reuse
```

The Executive exposes `create_push_invitation(identity_id, task_id)` and
`resume_with_push_invitation(identity_id, task_id, credential)` as controller
APIs. They are deliberately not identity capabilities, so a model cannot
approve its own write-back. The raw token is held in memory by the pusher and
is never saved in the task, identity state, artifact, receipt, or logs.

## Credential surface

Exactly one ephemeral bearer value is used. It is 32 random bytes encoded as
URL-safe text. The token is opaque: it reveals no identity, capability, path,
artifact digest, or expiration. Persistent storage contains only its SHA-256
digest and explicit scope. The invitation:

- expires after 15 minutes by default;
- can be used once;
- is revoked after five invalid token attempts;
- cannot authorize another identity, capability, or artifact revision;
- requires no API key, account credential, network access, or secret in the
  identity specification.

The current storage abstraction is single-node and has no compare-and-swap
primitive. Consumption is serialized within a process and persisted before
publication. Multi-node use requires a storage backend with an atomic claim;
this pipeline does not claim distributed one-time semantics.

## Evidence and overhead

Every accepted push stores a receipt under
`skill_forge.self_improvement.receipts`. It records:

- the one changed publication path and exact artifact SHA-256;
- published bytes and installed skill names;
- one ephemeral credential, zero long-lived credentials, and zero network calls;
- approval, publication, installation-verification, and total wall time;
- whether the operation completed or failed after approval.

Failures after invitation consumption are durable and do not silently invite a
replay. The published artifact is content-addressed and existing destinations
are never overwritten.

## Executable proof

`tests/test_self_improvement.py` proves the complete flow with JSON-file
persistence and three independently constructed registries. It verifies that:

1. the staged source is not importable and the identity is not installed before
   approval;
2. the invitation survives controller/runtime reconstruction;
3. the exact tested artifact is atomically published and installed;
4. a fresh registry reloads and invokes the capability successfully;
5. the raw token is absent from every persisted namespace;
6. reuse, expiration, wrong scope, invalid-token retries, and post-approval
   installation failure are rejected and recorded.

The Executive regression test additionally proves that a generated acquisition
task reaches a durable blocked state, accepts a controller-held invitation,
then completes the existing activate/invoke/persist/reload/reuse lifecycle.
