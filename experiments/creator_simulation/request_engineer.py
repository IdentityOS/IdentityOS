"""Request a real engineering proposal through Daedalus's configured engine."""
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from core.capabilities.daedalus.thinking_engine import ThinkingEngine

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(os.environ.get('IDOS_ENV_FILE', ROOT / '.env'))
manifest = json.loads((ROOT / 'registry/identities/lacebx/daedalus/manifest.json').read_text())
request = '''Design an executable creator-heavy IdentityOS user simulation. Most actors create identities with the public Python SDK, publish identities/capabilities, install each other's artifacts, and verify consumption after a fresh process restart. Consumers should have varied purposes. MiroFish may supply personas but cannot establish product execution. Propose a capability or harness using existing Skill Forge, SDK and registry APIs. No invented success. Current evidence: SDK Identity.create/load/export/from_file exists; SkillForge packages independently tested .idcap artifacts and install_artifact supports cross-identity reuse; CLI registry publish only prints instructions, registry install writes into repository .identity_store ignoring configurable storage and namespace directories may be missing. Top-level publish --registry-dir really writes manifest and index. Return JSON: approach, scenarios (at least 12, >=75% creators), capability_goal, limitations, acceptance_checks. Be critical about sandbox boundaries: Skill Forge disallows internal runtime imports except capability API. Do not claim implementation has executed.'''
thought = ThinkingEngine(preferred_provider='groq').think(manifest['personality']['system_prompt'], request, max_tokens=3500, temperature=0.1, max_retries=1)
record = {'request': request, 'identity': manifest['id'], 'provider': thought.provider, 'model': thought.model, 'duration_ms': thought.duration_ms, 'response': thought.content}
(ROOT / 'experiments/creator_simulation/engineering_response.json').write_text(json.dumps(record, indent=2))
print(json.dumps({k:v for k,v in record.items() if k not in ('request','response')}))
print(thought.content)
