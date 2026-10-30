# The same tiny model, two ways

This live demonstration uses the existing frozen IdentityOS benchmark. It asks
the same local model to complete five tasks first through Ollama alone, then
through IdentityOS. The five tasks cover arithmetic, remembered project facts,
capability use, persistence after restart, and an unknown personal fact. The
audience sees both successes and failures. No model answers are prewritten.

## First laptop run (October 30, 2026)

The first five-task run on an Intel i5-10210U laptop with 8 GB RAM and
`gemma3:270m` passed 3/5 on each path. Bare Ollama averaged 3.90 seconds per
task; IDOS averaged 9.33 seconds. IDOS recovered the project after restart
(`D01`), while bare failed that task. IDOS failed `A01` and `C01`, including
the multiplication capability task. This is one run, not a stable estimate or
a latency improvement. Its raw interaction records were generated locally and
should accompany any public claim; the run is reproducible with the command
below.

## Run it

Install IdentityOS, start Ollama, and pull a model that fits your machine.
From the repository root:

```powershell
python benchmarks/runner.py --mode both --model gemma3:270m --demo
```

The runner prints a separate summary for each side and writes raw task records
to `benchmarks/results/`. Open each run's `summary.md`, `results.json`, and
`interactions/` records. Show the actual capability call and result, the memory
retrieved after a new runtime instance, and any failed check. The frozen suite
and scoring rules are in `benchmarks/tasks/v0.1.0.json` and
`benchmarks/scoring.py`.

## Presentation sequence

1. Show `ollama list` and the model digest. State the CPU, RAM, context limit,
   and whether any other model was loaded.
2. Run bare and IDOS together. Keep the terminal visible throughout.
3. Open the five paired interaction records. For each, read the prompt,
   response, checks, capability evidence, and elapsed time.
4. Restart the process before the persistence probe and inspect the stored
   identity state. The runner's persistence tasks already do this.
5. End with the comparison table and the failures. Repeat at least three runs
   before describing a success-rate difference as stable.

## Claims the evidence can support

This demonstration can show that a persistent identity and verified tools let a
fixed model complete tasks it misses directly. It can also reveal overhead and
tool syntax failures. It does not prove a universal intelligence upgrade,
latency parity, cross-host communication, or capability use when the trace does
not show a real invocation. The old `qwen3:4b` published result is a different
model and hardware profile; never reuse its numbers for this 270M run.

For a broader show, run the full 30-task suite with the same command minus
`--demo`. Preserve the task suite, model digest, raw artifacts, and per-task
latencies. The existing IdentityBench endurance workflow can then demonstrate
whether memory, identity state, and goals survive repeated runs and restarts.
