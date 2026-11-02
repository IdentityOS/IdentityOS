# Tiny-model proof evidence — 2026-11-02

These are the durable records for the five-task public demo described in
[`tiny-model-proof.md`](../../tiny-model-proof.md). The paired run was executed
from the same checkout with:

```powershell
python benchmarks/runner.py --mode both --model gemma3:270m --demo --reset-identity
```

The reset flag intentionally wiped and recreated only the benchmark identity
before the IDOS side. The frozen demo selection was `A01`, `B01`, `C01`, `D01`,
and `F01` from benchmark suite v0.1.0.

## Environment

- Device: HP Pavilion x360 Convertible 14-dh1xxx
- CPU: Intel Core i5-10210U, 4 cores / 8 logical processors
- Physical RAM: 8,367,099,904 bytes
- Platform captured by runner: Windows 11, build 26200, 64-bit
- Python: 3.12.16
- Model: `gemma3:270m`, Gemma 3 268.10M, Q8_0
- Model digest: `e7d36fb2c3b3293cfe56d55889867a064b3a2b22e98335f2e6e8a387e081d6be`
- Model capabilities reported by Ollama: `completion`

The runner could not obtain CPU and memory text through its GNU command probes
on Windows, so those two fields in the raw environment are marked unavailable;
the Windows system inventory above fills that metadata gap. Local hostname and
absolute workspace path are the only redactions in the checked-in JSON. They do
not affect prompts, outputs, checks, scores, latencies, model identity, or runner
configuration. Each JSON file embeds the complete five task records, so the
interaction evidence can be audited without a separate summary.

## Artifacts

| Artifact | Run ID | SHA-256 |
|---|---|---|
| [`bare-results.json`](bare-results.json) | `bare-20261102T171220Z` | `14bcc179464344de653490a54370d2c51ec985b07c27d3424c7111161f53dcbf` |
| [`idos-results.json`](idos-results.json) | `idos-20261102T171243Z` | `e56dd28f2dafa08d0acc55bf30216e87b2a91a786dcbffd2caad99285c84cc86` |

This single paired run supports a demonstration and falsifiable inspection. It
does not establish a stable success-rate improvement; repeat at least three
clean pairs before making that stronger claim.
