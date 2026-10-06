"""Ask Daedalus to implement an SDK lifecycle probe; verify through Skill Forge."""
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from core.capabilities.daedalus.thinking_engine import ThinkingEngine
from core.skill_forge import AcceptanceCase, ForgeRequest, SkillForge, ModelCapabilityAuthor

ROOT = Path(__file__).resolve().parents[2]
class Adapter:
    def generate(self, context, user_input, **kwargs):
        load_dotenv(os.environ.get('IDOS_ENV_FILE', ROOT / '.env'))
        persona = json.loads((ROOT / 'registry/identities/lacebx/daedalus/manifest.json').read_text())['personality']['system_prompt']
        example = (ROOT / 'tests/test_skill_forge.py').read_text().split('def manifest(')[0]
        thought = ThinkingEngine('groq').think(persona + '\n' + context + '\nActual capability API example:\n' + example, user_input, max_tokens=6500, temperature=0.1, max_retries=1)
        (ROOT / 'experiments/creator_simulation/author_response.json').write_text(json.dumps({'provider':thought.provider,'model':thought.model,'duration_ms':thought.duration_ms,'response':thought.content},indent=2))
        return thought.content
class Designer:
    def design(self, *args):
        raise AssertionError('Independent cases already supplied')

goal = '''Implement creator_probe.run with required input text:string. Execute real public SDK behavior using from identityos import Identity and tempfile.TemporaryDirectory. Create identity_id='probe', name='CreatorProbe', storage_path=tempdir; remember(text); goal(text); export(tempdir+'/identity.json'); load the same identity using Identity.load('probe', storage_path=tempdir). Inspect recalled memories via loaded.memories() and goals via loaded.goals('all'). Return data {'text':text,'memory_persisted':bool,'goal_persisted':bool,'export_exists':bool} based on observed values. Use pathlib.Path to check export exists. Fail if any verification false. No environment mutation, no network, no subprocess, no arbitrary input paths. This is a small SDK probe to be used by an external multi-user harness. Do not invent SDK methods. Identity.memories() returns list of dictionaries with content; goals returns dictionaries with title. Use skill permission='filesystem', include verification_params={'text':'probe'} and an object_schema requiring text. Import only allowed modules and actual capability API. Define normal install/uninstall/prompts/skills/call methods. Previous candidate failed: its Skill permission was local despite required filesystem. Set Skill(permission="filesystem") explicitly; manifest permissions alone do not declare skill permission. permissions=['filesystem'], dependencies=['identityos','tempfile'].'''
goal += '\nPrevious attempts invented Identity constructors. There is NO Identity constructor. Use exactly Identity.create(name="CreatorProbe", identity_id="probe", storage_path=tmp_dir). Manifest skills MUST be objects: [{"name":"creator_probe.run","description":"SDK lifecycle probe"}], not strings. Use tempfile.TemporaryDirectory, no mkdtemp or cleanup imports.'
request = ForgeRequest('creator_probe', goal, 'daedalus', allowed_permissions=('filesystem',), allowed_dependencies=('identityos','tempfile'), acceptance_cases=tuple(AcceptanceCase(name=t,skill='creator_probe.run',params={'text':t},expected={'text':t,'memory_persisted':True,'goal_persisted':True,'export_exists':True},grants=('filesystem',)) for t in ('Publish a poetry editor','Build a research assistant')))
forge = SkillForge(ROOT, author=ModelCapabilityAuthor(Adapter()), test_designer=Designer(), timeout_seconds=45)
try:
    result=forge.forge(request)
    record={'passed':True,**result.to_dict()}
except Exception as exc:
    record={'passed':False,'error':f'{type(exc).__name__}: {exc}'}
(ROOT / 'experiments/creator_simulation/forge_result.json').write_text(json.dumps(record,indent=2))
print(json.dumps(record,indent=2))
