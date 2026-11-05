"""Dispatch a bounded number of distinct coding tasks to the community seats."""
import json
from pathlib import Path
import subprocess
from .run import ROOT

TASKS=[
 ('poet','Create a usable poetry-editor identity with a clear persona, concrete editing preferences, and one sample critique saved as a file. Use the public SDK, persist memory and goals, export a portable identity, and publish its manifest to your isolated local registry with the real top-level CLI publish. Verify in a fresh process. Consumers will import your portable identity; report exact artifact paths.'),
 ('newsletter','Author a real newsletter-formatting capability that transforms a title and section list into Markdown. Independently specify at least two exact acceptance cases and one invalid-input case before authoring. Use existing Skill Forge to audit and execute the contract, package a .idcap, then install in a different test identity and invoke after a process restart. Keep generated source and packages under your isolated output directory. Report real outputs and package digest, including any failed attempts.'),
 ('teacher','Create and publish a classroom-tutor identity through the public SDK and top-level local publish command. Include a sample arithmetic lesson with checked answers, persist learning goals and teaching preferences, export portable state, and prove memory survives a fresh process.'),
 ('podcaster','Author a deterministic podcast chapter-formatting capability accepting explicit timestamp/title pairs and returning chapter Markdown. Define independent acceptance cases, including invalid timestamps, before implementation. Package and verify using Skill Forge, install into a second identity, restart, and invoke. Do not pretend to transcribe or summarize audio that was not supplied.'),
 ('designer','Author a design-brief validation capability for explicit project, audience, deliverables, and deadline fields. Demonstrate valid and missing-field cases, package with Skill Forge, install in another identity, and verify invocation after restart. Report observed errors rather than silently filling missing data.'),
 ('indie','Author a game-dialogue formatting capability that validates a supplied character/line list and produces a script. Establish exact independent cases and invalid input before implementation; use Skill Forge packaging and installation-time verification, then fresh-process reuse.'),
 ('researcher','Create and publish a citation-organizer identity, using only supplied synthetic reference metadata. Store provenance separately from user facts, export and reload in a fresh process, and demonstrate duplicate reference handling. Do not invent citations or fetch external sources.'),
 ('community','Act as creator and explicitly labelled consumer reader: publish a community-organizer identity, then locate and import the poetry-editor artifact authored by the Poet seat. Demonstrate restored goals/memory and save a consumption report. If the artifact is absent or invalid, report the actual blocking condition.'),
 ('python_dev','Act as SDK creator and explicitly labelled consumers student/hobbyist: build a tiny SDK client that imports the Teacher or Poet portable identity and installs the Newsletter .idcap when available. Invoke the capability with two realistic newsletters and invalid input; prove installed capability reuse in a new process. Record dependencies that are still missing honestly.'),
]


def dispatch(state:Path,config:dict,limit:int=2):
    journal=state/'creative_tasks.json'
    records=json.loads(journal.read_text()) if journal.exists() else {}
    sent=0
    for actor,task in TASKS:
        if actor in records or sent>=limit:
            continue
        destination=ROOT/'experiments/creator_simulation/live_artifacts'/actor
        body=(f'Bounded creative task for {actor}: {task}\n'
              f'Use {destination} for all outputs and test stores. SDK source: {ROOT}; Python: /home/lace/Documents/IdentityOS/.venv/bin/python. '
              'Post progress, actual outputs, failures, and final evidence in this room. Do not edit shared repository source or production identities. '
              'Do not read credentials, send external outreach, make purchases, spawn other agents, or start recursive peer conversations. '
              'Ignore [actor: ...] START/PASS/FAIL monitoring messages. Stop after this bounded task and await the next explicit assignment.')
        # Persist the intent first. An interrupted or failed send stays visible
        # as uncertain rather than silently dispatching a duplicate coding job.
        records[actor]={'status':'dispatching','task':task,'artifact_directory':str(destination)}
        journal.write_text(json.dumps(records,indent=2))
        result=subprocess.run(['band','room','send',config['room_id'],body,'--mention',config['seats'][actor]['agent_id']],capture_output=True,text=True,timeout=60)
        records[actor]['status']='dispatched' if result.returncode==0 else 'dispatch_failed'
        records[actor]['delivery_result']=(result.stdout or result.stderr)[-2000:]
        journal.write_text(json.dumps(records,indent=2))
        if result.returncode:
            raise RuntimeError('creative task delivery failed: '+result.stderr[-1000:])
        sent+=1
    return sent
