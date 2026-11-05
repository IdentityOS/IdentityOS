"""Executable synthetic community. Each actor runs in fresh isolated processes."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
ACTORS = [
    ('poet', 'identity_creator', 'poetry editor'),
    ('teacher', 'identity_creator', 'classroom tutor'),
    ('researcher', 'identity_creator', 'citation organizer'),
    ('community', 'identity_creator', 'community moderator'),
    ('newsletter', 'capability_creator', 'newsletter formatting'),
    ('podcaster', 'capability_creator', 'episode summaries'),
    ('designer', 'capability_creator', 'design brief transformation'),
    ('indie', 'capability_creator', 'game dialogue tooling'),
    ('python_dev', 'sdk_creator', 'Python companion app'),
    ('automation', 'sdk_creator', 'workflow automation'),
    ('accessibility', 'sdk_creator', 'accessible reading app'),
    ('integrator', 'sdk_creator', 'team knowledge app'),
    ('reader', 'consumer', 'reading published poetry'),
    ('student', 'consumer', 'learning with a tutor'),
    ('hobbyist', 'consumer', 'organizing personal projects'),
    ('operator', 'consumer', 'using community tools'),
]

def worker(action, actor, purpose, home, artifact):
    from identityos import Identity
    store = str(home / 'store')
    if action == 'create':
        identity = Identity.create(actor, identity_id=actor, persona=purpose, storage_path=store)
        identity.remember(purpose)
        identity.goal(purpose)
        identity.export(str(home / 'portable.json'))
        return {'id': identity.id, 'export_exists': (home / 'portable.json').is_file()}
    identity = Identity.load(actor, storage_path=store)
    if action == 'restart':
        assert any(m['content'] == purpose for m in identity.memories()), 'memory lost'
        assert any(g['title'] == purpose for g in identity.goals('all')), 'goal lost'
        return {'memory_persisted': True, 'goal_persisted': True}
    if action == 'consume_identity':
        source = home.parent / 'poet' / 'portable.json'
        restored = Identity.from_file(str(source))
        assert restored.name == 'poet'
        assert any(g['title'] == 'poetry editor' for g in restored.goals('all'))
        return {'source_actor': 'poet', 'name': restored.name, 'goals_restored': True}
    if action == 'author_capability':
        from core.skill_forge import SkillForge, ForgeProposal, ForgeRequest, AcceptanceCase
        from core.skill_forge.artifacts import package_record
        seed = package_record(artifact)
        cap_id = actor + '_probe'
        source = seed['source'].replace('creator_probe', cap_id)
        manifest = dict(seed['manifest'], id=cap_id, author=actor,
                        skills=[{'name': cap_id+'.run', 'description': purpose}])
        class Author:
            def author(self, request, identity=None):
                return ForgeProposal(source, manifest)
        cases = tuple(AcceptanceCase(name=t, skill=cap_id+'.run', params={'text':t},
                      expected={'text':t,'memory_persisted':True,'goal_persisted':True,'export_exists':True},
                      grants=('filesystem',)) for t in (purpose, 'independent reuse example'))
        forge = SkillForge(home/'authored', author=Author(), test_designer=None, timeout_seconds=45)
        result = forge.forge(ForgeRequest(cap_id, purpose, actor, allowed_permissions=('filesystem',),
                         allowed_dependencies=('identityos','tempfile'), acceptance_cases=cases))
        published = home.parent/'capabilities'
        published.mkdir(exist_ok=True)
        target = published/(cap_id+'.idcap')
        target.write_bytes(Path(result.artifact_path).read_bytes())
        return {'published':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
                'authoring_method':'template adaptation of verified engineer source'}
    from core.skill_forge.artifacts import package_record
    cap_id = package_record(artifact)['manifest']['id']
    if action == 'install':
        from core.skill_forge import SkillForge
        forge = SkillForge(home / 'forge', author=None, test_designer=None, timeout_seconds=45)
        # Installation is the actual platform API; SDK has no install wrapper.
        forge.install_artifact(artifact, identity_id=actor,
            capability_registry=identity._runtime.capability_registry, storage=identity._runtime._storage)
        denied=identity.call(cap_id+'.run',text=purpose)
        assert not denied.success, 'filesystem permission was not enforced'
        identity._runtime.capability_registry.grant(actor, cap_id, 'filesystem')
        assert identity.can(cap_id+'.run')['available'], 'installed skill unavailable'
        return {'installed': True, 'capability_id':cap_id, 'permission_denied_before_grant':True,
                'artifact_sha256':hashlib.sha256(artifact.read_bytes()).hexdigest()}
    if action == 'invoke':
        result = identity.call(cap_id+'.run', text=purpose)
        assert result.success, result.error
        assert all(result.data[k] for k in ('memory_persisted','goal_persisted','export_exists'))
        return {'capability_id':cap_id, **result.data}
    if action == 'invalid_call':
        result = identity.call(cap_id+'.run')
        assert not result.success, 'invalid input was accepted'
        return {'expected_failure': True, 'error': result.error}
    raise ValueError(action)


def run(output: Path, artifact: Path, on_event=None):
    output.mkdir(parents=True, exist_ok=False)
    ledger=[]
    def observe(actor, action, cmd, home, check=None):
        if on_event:
            on_event({"actor":actor,"action":action,"phase":"starting"})
        started=time.monotonic()
        env=dict(os.environ, PYTHONPATH=str(ROOT), IDENTITY_STORE_PATH=str(home/'import_store'))
        env.pop('IDENTITY_REGISTRY_URL', None)
        completed=subprocess.run(cmd,cwd=home,env=env,capture_output=True,text=True,timeout=90)
        record={'actor':actor,'action':action,'exit_code':completed.returncode,
                'passed':completed.returncode==0,'duration_ms':round((time.monotonic()-started)*1000,2),
                'stdout':completed.stdout[-10000:],'stderr':completed.stderr[-3000:]}
        if check and record['passed']:
            try:
                check()
            except Exception as exc:
                record['passed']=False
                record['verification_error']=str(exc)
        ledger.append(record)
        with (output/'ledger.jsonl').open('a') as stream:
            stream.write(json.dumps(record)+'\n')
        print(f"{actor}: {action}: {'PASS' if record['passed'] else 'FAIL'}",flush=True)
        if on_event:
            on_event({**record,"phase":"finished"})
        return record['passed']
    for actor, role, purpose in ACTORS:
        home=output/actor
        home.mkdir()
        selected_artifact=artifact
        shared=output/'capabilities'/'newsletter_probe.idcap'
        if shared.exists() and role != 'capability_creator':
            selected_artifact=shared
        def command(action):
            return [sys.executable,str(Path(__file__).resolve()),'--worker',action,'--actor',actor,
                    '--purpose',purpose,'--home',str(home),'--artifact',str(selected_artifact)]
        if not observe(actor,'create',command('create'),home):
            continue
        observe(actor,'fresh_process_reload',command('restart'),home)
        if role=='identity_creator':
            manifest=output/'registry'/'identities'/actor/actor/'manifest.json'
            def verify_publish():
                assert json.loads(manifest.read_text())['name']==actor
                index=json.loads((output/'registry'/'index.json').read_text())
                assert any(item['id']==f'{actor}/{actor}' for item in index['identities'])
            observe(actor,'publish_identity',[sys.executable,'-m','cli.main','--store',str(home/'store'),
                    'publish','--id',actor,'--author',actor,'--registry-dir',str(output/'registry')],home,verify_publish)
        if role=='capability_creator':
            observe(actor,'author_and_publish_capability',command('author_capability'),home)
        if role=='consumer' or role=='sdk_creator':
            observe(actor,'consume_shared_identity',command('consume_identity'),home)
        observe(actor,'install_engineer_capability',command('install'),home)
        observe(actor,'invoke_after_process_restart',command('invoke'),home)
        observe(actor,'reject_invalid_input',command('invalid_call'),home)
    summary={'actors':len(ACTORS),'creators':sum(role!='consumer' for _,role,_ in ACTORS),
             'creator_ratio':sum(role!='consumer' for _,role,_ in ACTORS)/len(ACTORS),'steps':len(ledger),'passed':sum(r['passed'] for r in ledger),
             'failures':[{'actor':r['actor'],'action':r['action'],'error':r.get('verification_error') or r['stderr'] or r['stdout']} for r in ledger if not r['passed']],
             'artifact_sha256':hashlib.sha256(artifact.read_bytes()).hexdigest(),
             'limitations':['Synthetic workload; no user adoption predictions.',
                 'Python SDK exercised; JavaScript SDK and model chat are not covered.',
                 'Identity consumption uses portable SDK import; registry install CLI remains unverified.',
                 'Four capability creators adapt the engineer-authored template; independent human coding behavior is not modeled.']}
    (output/'summary.json').write_text(json.dumps(summary,indent=2))
    (output/'report.md').write_text(f"# Creator community execution report\n\n{summary['actors']} actors, {summary['creators']} creators ({summary['creator_ratio']:.0%}).\n\n{summary['passed']}/{summary['steps']} observed steps passed.\n\n"+'\n'.join(f"- {x}" for x in summary['limitations'])+'\n\nFailures:\n'+json.dumps(summary['failures'],indent=2))
    return summary

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path)
    parser.add_argument('--artifact',type=Path,required=True)
    parser.add_argument('--worker')
    parser.add_argument('--actor');parser.add_argument('--purpose');parser.add_argument('--home',type=Path)
    args=parser.parse_args()
    if args.worker:
        print(json.dumps({'pid':os.getpid(),'observed':worker(args.worker,args.actor,args.purpose,args.home,args.artifact)}))
    else:
        summary=run(args.output.resolve(),args.artifact.resolve())
        print(json.dumps(summary,indent=2))
        raise SystemExit(bool(summary['failures']))
