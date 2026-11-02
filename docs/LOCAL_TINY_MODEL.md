# Local tiny-model profile

IdentityOS can reduce the amount of context and capability material passed to a
small local model. The interactive chat CLI now accepts `--context-tokens`,
`--max-tools`, and `--tool-result-chars`; defaults retain previous behavior.
For an Ollama model on a memory-constrained CPU laptop, start with 1200 context
tokens, two relevant tools, and 1200 characters per tool result. Keep Ollama's
own context limit at least as large as the IdentityOS context budget plus the
user turn and tool descriptions. This is an operational profile, not an accuracy
claim.

On Windows, `scripts/local_tiny_profile.ps1 -Model gemma3:270m` starts chat
with that profile. `-RunBenchmark` runs the existing frozen five-task demo
through both the bare and IDOS paths, without freezing or changing the suite.
Inspect each run's artifacts under `benchmarks/results/`; compare success and
latency before claiming a win. Tool calls can add a model round trip, and a
270M model may still fail to emit valid tool syntax.

The repository already has a Karpathy-style `benchmarks/autopilot.py` and
`benchmarks/ratchet.py`. The autopilot can now request proposals from a local
Ollama coder with `--provider ollama --model <coder-model>`. The coder model is
separate from the frozen evaluation model. The ratchet accepts an edit only
after the full locked suite, tests, hallucination gate, and latency gate pass.
Local proposals must be valid JSON; failures stop instead of silently reusing
an old proposal. The 270M model has not demonstrated reliable code editing,
so use `--plan-only` first and inspect the proposal before an experiment. The
current ratchet lock is for SmolLM2-360M, not this laptop's Gemma model; a new
versioned frozen baseline is required before claiming Gemma ratchet wins.
