# Ollama and synchronous generation deadlines

IdentityOS distinguishes two timeout contracts:

- a provider/HTTP timeout bounds a connection phase or a period of inactivity;
- `_generation_budget` bounds the caller's total wall-clock wait for one model
  interaction, including lazy client construction, Ollama `/api/show` probing,
  provider retries, and all tool rounds.

A synchronous SDK call cannot be safely interrupted by raising an exception in
its Python thread. Budgeted calls therefore run in a daemon worker while the
calling thread waits on a monotonic deadline. On expiry, the caller immediately
receives `GenerationDeadlineError` (both a `TimeoutError` and the historical
adapter-facing `RuntimeError`), and cleanup detaches and closes the active
client asynchronously. The worker may exist briefly while that transport close
unblocks it; because it is a daemon, it cannot hold process shutdown open.

The outer deadline alone is not an effects boundary. OpenAI-compatible and
Ollama tool loops also check the same deadline immediately before every runtime
tool callback. A response that arrives after expiry therefore cannot execute a
capability. Normal calls without `_generation_budget` do not create a worker and
retain their existing synchronous behavior.

`tests/test_adapter_deadline.py` covers normal completion, exception identity,
bounded timeout return, cleanup and worker exit, and the late-tool guard.
`tests/test_adapters.py` additionally uses a continuously chunking HTTP server
and slow client/probe simulations to verify the complete adapter boundary.
