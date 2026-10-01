# Cross-device embodiment

Cross-device embodiment lets one identity route camera, voice, browser, and
desktop actions through a single durable Executive task. The device layer does
not create another planner or another identity state. Every step retains the
same identity ID, task ID, checkpoint, status, and evidence chain.

## Trust boundary

Device adapters are attached and authorized through the trusted host API,
`EmbodimentHub`; those operations are intentionally not model-callable. An
authorization pins the device descriptor digest and a precise action allowlist.
Changing a descriptor, detaching a device, revoking an authorization, or losing
an underlying capability grant makes execution fail closed.

The marketplace `embodiment` capability exposes only:

- `embodiment.list_devices`, which reports authorized attachment readiness;
- `embodiment.start_task`, which preflights every step before queuing one
  durable task; and
- `embodiment.task_status`, which reads the Executive's persisted state.

The `embodiment:execute` permission is required to start work. Each device has
its own second authorization boundary, and capability-backed devices also pass
through the normal capability installation, permission, and parameter gateway.
The runtime attaches browser and desktop capability bridges as
`browser_runtime` and `desktop_runtime`; a host must still authorize them for a
specific identity. Camera and voice hosts attach drivers implementing the same
`DeviceAdapter` contract.

## Evidence and failure behavior

An adapter must return a `DeviceObservation`. The hub persists its source,
evidence class, data digest, task ID, identity ID, and bounded structured data.
Observations larger than 64 KiB are rejected. Durable device task parameters
with secret-bearing keys are rejected rather than written to task storage.
Adapter observations are also sanitized before they become task evidence:
values beneath token, password, credential, authorization, cookie, API-key, or
secret-shaped keys are replaced with `[REDACTED]` before hashing and persistence.

Evidence classes are explicit: `hardware`, `runtime`, or `simulated`. A device
not declared hardware-backed cannot claim a hardware observation. Test doubles
therefore remain visibly simulated; their output is never presented as proof of
a physical camera, microphone, or speaker action.

Read-only actions explicitly declared replay-safe may be retried. Mutating or
executing actions get one attempt. If a process stops while such an action is in
flight, normal Executive recovery blocks for manual reconciliation instead of
silently repeating an external effect. A task whose next device is merely
offline blocks as resumable and can continue after the same descriptor is
reattached, preserving already-completed steps.
