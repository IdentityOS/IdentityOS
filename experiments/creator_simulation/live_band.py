"""One durable community round, with each actor's work reported through its BAND seat."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from . import run as simulation


def band(args, *, session=None, parse=False):
    cmd=['band','--config-dir',str(Path.home()/'.jam'),'--profile','default']
    if session:
        cmd += ['--session',session]
    result=subprocess.run(cmd+args,capture_output=True,text=True,timeout=60,check=True)
    return json.loads(result.stdout) if parse else result.stdout.strip()


def provision(state):
    state.mkdir(parents=True,exist_ok=True)
    path=state/'bindings.json'
    config=json.loads(path.read_text()) if path.exists() else {'seats':{}}
    for actor,role,purpose in simulation.ACTORS:
        if actor not in config['seats']:
            session='idos-users-'+actor
            try:
                brief=band(['brief','--json'],session=session,parse=True)
            except subprocess.CalledProcessError:
                band(['onboard','--host','generic','--name','IDOS '+actor.replace('_',' ').title(),
                      '--receiver','pull','--cwd',str(simulation.ROOT)],session=session)
                brief=band(['brief','--json'],session=session,parse=True)
            config['seats'][actor]={'session':session,'handle':brief['target']['handle'],
                                   'agent_id':brief['target']['agent_id'],'role':role,'purpose':purpose}
            path.write_text(json.dumps(config,indent=2))
            print('Provisioned',actor,flush=True)
    owner=config['seats']['poet']['session']
    room=config.get('room_id')
    if not room:
        raise RuntimeError('Set an explicitly selected room_id in bindings.json before connecting seats')
    # Agent access must exist before any workload starts. The human owner adds
    # the first seat; that seat may then invite the remaining dedicated seats.
    band(['chat','participants',room,'--json'],session=owner,parse=True)
    for actor,seat in config['seats'].items():
        if seat.get('shared_seat'):
            continue
        if actor!='poet':
            try:
                band(['invite','--room',room,'--session-id','creator-users-'+actor,'--force'],session=owner)
            except subprocess.CalledProcessError as exc:
                if '409' not in (exc.stderr or ''):
                    raise RuntimeError('BAND seat invitation failed: '+(exc.stderr or '').strip()) from exc
    participants=band(['chat','participants',room,'--json'],session=owner,parse=True)
    config['verified_participants']=participants
    path.write_text(json.dumps(config,indent=2))
    print(json.dumps({'room_id':room,'actors':len(config['seats']),
                      'dedicated_seats':sum(not s.get('shared_seat') for s in config['seats'].values())},indent=2))
    return config



class RoomJournal:
    def __init__(self,state,config):
        self.config=config
        self.db=sqlite3.connect(state/'room_outbox.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, actor TEXT, body TEXT, delivered INTEGER DEFAULT 0)')
        self.db.commit()

    def post(self,key,actor,body):
        self.db.execute('INSERT OR IGNORE INTO outbox(id,actor,body) VALUES(?,?,?)',(key,actor,body))
        self.db.commit()
        self.flush()

    def flush(self):
        for key,actor,body in self.db.execute('SELECT id,actor,body FROM outbox WHERE delivered=0 ORDER BY rowid').fetchall():
            seat=self.config['seats'][actor]
            other='teacher' if seat['session']==self.config['seats']['poet']['session'] else 'poet'
            recipient=self.config['seats'][other]['handle']
            band(['send',self.config['room_id'],f'@{recipient} [actor: {actor}] '+body],session=seat['session'])
            self.db.execute('UPDATE outbox SET delivered=1 WHERE id=?',(key,))
            self.db.commit()


def round_once(state):
    config=provision(state)
    journal=RoomJournal(state,config)
    journal.flush()
    progress=state/'progress.json'
    previous=json.loads(progress.read_text()) if progress.exists() else {'round':0}
    if previous.get('status')=='running':
        journal.post(f"interrupted:{previous['round']}",'poet',
                     f"Round {previous['round']} was interrupted. Its partial ledger remains on disk. Starting a fresh round; no unfinished step is reported as passed.")
    count=previous['round']+1
    progress.write_text(json.dumps({'round':count,'status':'running','pid':os.getpid(),'started':time.time()}))
    journal.post(f'intro:{count}','poet',
        f"IDOS creator community — round {count}. 12 creators and 4 consumers are executing Python SDK, local publishing, installation, and restart checks. These are synthetic scripted workloads, not autonomous human users. Each seat posts START and observed PASS/FAIL for every step. A durable timer schedules another round 10 minutes after completion. No external outreach or model calls are performed.")
    for actor,seat in config['seats'].items():
        if count==1:
            journal.post('seat:'+actor,actor,f"I am the synthetic {seat['role']} seat for {seat['purpose']}. My messages report commands the harness actually executes, including failures.")
    def event(record):
        actor=record['actor'];phase=record['phase'];action=record['action']
        if phase=='starting':
            body=f"Round {count} · START {action}. Purpose: {config['seats'][actor]['purpose']}."
        else:
            outcome='PASS' if record['passed'] else 'FAIL'
            output=record['stdout']
            try:
                output=json.dumps(json.loads(output).get('observed'),ensure_ascii=False)
            except (ValueError,AttributeError):
                pass
            body=f"Round {count} · {outcome} {action} ({record['duration_ms']} ms; exit {record['exit_code']}). Observed: {output[:2000]}"
            if not record['passed']:
                body+='\nFailure: '+(record.get('verification_error') or record['stderr'])[:1500]
        journal.post(f'{count}:{actor}:{action}:{phase}',actor,body)
    output=state/'runs'/f'round-{count:06d}'
    output.parent.mkdir(exist_ok=True)
    result=simulation.run(output,simulation.ROOT/'experiments/creator_simulation/artifacts/creator_probe.idcap',on_event=event)
    journal.post(f'done:{count}','poet',f"Round {count} finished: {result['passed']}/{result['steps']} checks passed. Failures: {json.dumps(result['failures'])[:2000]}. Evidence: {output}. Next round is scheduled in 10 minutes.")
    progress.write_text(json.dumps({'round':count,'status':'completed' if not result['failures'] else 'failed','finished':time.time(),'summary':result}))
    journal.db.close()
    return bool(result['failures'])

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['provision','round'])
    parser.add_argument('--state',type=Path,required=True)
    args=parser.parse_args()
    state=args.state.resolve()
    if args.command=='provision':
        provision(state)
    else:
        try:
            raise SystemExit(round_once(state))
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            reason=(exc.stderr or str(exc)) if isinstance(exc,subprocess.CalledProcessError) else str(exc)
            state.mkdir(parents=True,exist_ok=True)
            (state/'readiness.json').write_text(json.dumps({'status':'readiness_or_delivery_failed','error':reason[-2000:],'checked_at':time.time(),'pid':os.getpid()}))
            print(reason,flush=True)
            raise SystemExit(1)
