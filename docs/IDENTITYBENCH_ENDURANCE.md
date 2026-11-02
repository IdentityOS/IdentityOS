# Long-running IdentityBench

IdentityBench combines simulated multi-week worlds with durable health samples
captured by recurring GitHub Actions. Every successful benchmark run records an
endurance sample in `.identitybench/endurance/<identity>.json`; nightly, weekly,
and monthly workflows restore that state and extend the same history.

```bash
identitybench run benchmark-bot --mode smoke
identitybench endurance record benchmark-bot
identitybench endurance report benchmark-bot -o endurance.md
```

Each sample uses runtime-observed state to measure identity-core consistency,
memory count and growth, goal completion, relationship stability, prompt size,
pipeline latency, benchmark hallucination rate, and recovery through a new
runtime instance. Restart recovery compares identity, memory, goals,
intentions, relationships, and timeline before and after reload.

The Markdown report includes a Mermaid trend graph and structured alerts.
Default degradation thresholds cover identity drift, relationship churn,
hallucination rate, restart loss, latency growth, prompt growth, and benchmark
score drops. Missing checks are marked unobserved rather than assigned a
plausible neutral score, and a benchmark without a configured model adapter
fails instead of scoring the runtime's placeholder response.

Provider-backed runs use a bounded resource profile so capability output cannot
silently consume the next model request. The defaults can be tuned explicitly:

```bash
IDENTITYBENCH_CONTEXT_TOKENS=1200 \
IDENTITYBENCH_RESPONSE_TOKENS=256 \
IDENTITYBENCH_TOOL_RESULT_CHARS=1200 \
IDENTITYBENCH_REQUEST_TIMEOUT_SECONDS=30 \
IDENTITYBENCH_REQUEST_INTERVAL_SECONDS=35 \
identitybench run benchmark-bot --mode smoke
```

For a local Ollama run, use a total interaction deadline rather than relying on
the HTTP timeout of each individual tool round:

```bash
IDENTITY_ADAPTER=ollama \
IDENTITY_ADAPTER_CONFIG='{"model":"qwen3:4b","temperature":0,"max_tokens":192}' \
identitybench run benchmark-bot --mode smoke --request-timeout 30
```

Ollama tool support is detected from its `/api/show` model metadata. Models that
advertise `tools` use native structured calls; other models receive a compact
text protocol. Set `OLLAMA_PREFER_LEGACY_TOOLS=1` only to diagnose or work around
a model-specific native parser problem. The runtime relevance-bounds both tool
schemas and their capability prompts, so installing more capabilities does not
make every unrelated interaction progressively slower.

The request interval defaults to zero for providers without a benchmark-specific
policy and to 35 seconds for Groq, where a single interaction may require more
than one provider request to complete a capability call. Every run records the
effective public adapter/model and resource profile in its result; credentials
are never included.

Hosted PR and scheduled benchmarks use Groq's `openai/gpt-oss-20b` as the
canonical resource-bounded baseline. This keeps the engineering signal on a
supported tool-capable model while leaving the runtime's normal model default
unchanged. Cache histories are isolated by model and run type (PR, nightly,
weekly, and monthly), so comparisons never silently mix model families or
benchmark modes. Changing the hosted baseline therefore starts a new history
instead of presenting a model change as a product regression or improvement.

Every result records a scoring-schema version, a SHA-256 fingerprint of the
executable benchmark suite, and a comparison signature covering the suite,
model, seed, worlds, and resource budgets. Regression analysis refuses to
compare mismatched signatures. CI cache chains use the same suite and workflow
hash plus a unique run ID, preserving new history without restoring results
from changed scoring code. Trusted pushes to `main` publish the reusable PR
baseline because GitHub scopes caches created by a pull request to that PR's
synthetic merge ref; later PRs restore the `main` history rather than accepting
state written by a sibling PR. Uploaded artifacts explicitly include the
hidden `.identitybench` directory; the raw world transcripts and configuration
remain available alongside the human-readable report as execution evidence.
After a cache restore, workflows inspect the persisted identity by its explicit
ID and only create it when it is genuinely absent. This preserves the stable
creation fingerprint used by the identity-consistency metric.

Every completed run now carries a `champion_baseline` assessment. The champion
is replayed from all independently rescorable runs with the exact same suite,
model, seed, worlds, and resource profile. It is monotonic: a challenger must
increase the overall score without a truth/isolation guardrail regression, a
policy failure, or a world regression beyond the configured budget. A lower,
invalid, or unsafe run is recorded but never becomes the next baseline. The
champion comparison is included in every PR benchmark comment.

This high-water mark remains an advisory observation, not promotion evidence.
Model-backed smoke scores are noisy enough that one unpaired observation cannot
distinguish a code regression from provider/model variance, and the luckiest
single run must not gain merge authority. Unit, integration,
runtime-execution, evidence-upload, and security failures remain hard PR
failures. Statistical acceptance belongs to the independently rescored
multi-pair protected gate described below.

The proposed protected evaluator, multiple-daily schedule, paired statistical
gate, and autonomous improvement workflow are specified in
[`IDENTITYBENCH_INTEGRITY.md`](IDENTITYBENCH_INTEGRITY.md).
