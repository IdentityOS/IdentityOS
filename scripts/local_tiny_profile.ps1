param([string]$Model = 'gemma3:270m', [switch]$RunBenchmark)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$identity = Join-Path $root '.venv\Scripts\identity.exe'
if (-not (Test-Path $python)) { throw 'Install the project virtual environment first.' }
$env:OLLAMA_API_KEY = 'ollama'
$env:OLLAMA_BASE_URL = 'http://127.0.0.1:11434/v1'
$env:OLLAMA_MODEL = $Model
Set-Location $root
if ($RunBenchmark) {
    & $python benchmarks/runner.py --mode both --model $Model --demo
} else {
    & $identity chat --id local-bridge --adapter ollama --model $Model --context-tokens 1200 --max-tools 2 --tool-result-chars 1200
}
