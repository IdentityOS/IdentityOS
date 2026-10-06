"""Durable reporting must precede work, and failed sends must remain pending."""
import json
import subprocess
import pytest
from experiments.creator_simulation import live_band


def bindings():
    return {'room_id':'test-room','seats':{'poet':{'session':'poet','handle':'owner/poet'},'teacher':{'session':'teacher','handle':'owner/teacher'}}}


def test_failed_delivery_is_replayed_without_losing_event(tmp_path,monkeypatch):
    journal=live_band.RoomJournal(tmp_path,bindings())
    def fail(*args,**kwargs):
        raise subprocess.CalledProcessError(1,['band'],stderr='offline')
    monkeypatch.setattr(live_band,'band',fail)
    with pytest.raises(subprocess.CalledProcessError):
        journal.post('event-1','poet','START publish')
    assert journal.db.execute('SELECT delivered FROM outbox').fetchone()[0]==0
    calls=[]
    monkeypatch.setattr(live_band,'band',lambda args,**kw:calls.append((args,kw)))
    journal.flush()
    journal.post('event-1','poet','START publish')
    assert len(calls)==1
    assert '[actor: poet]' in calls[0][0][-1]
    assert journal.db.execute('SELECT delivered FROM outbox').fetchone()[0]==1
    journal.db.close()


def test_unreachable_room_prevents_work(tmp_path,monkeypatch):
    (tmp_path/'bindings.json').write_text(json.dumps(bindings()))
    monkeypatch.setattr(live_band,'provision',lambda state: (_ for _ in ()).throw(RuntimeError('room unreachable')))
    monkeypatch.setattr(live_band.simulation,'run',lambda *a,**k:pytest.fail('work began before room access'))
    with pytest.raises(RuntimeError,match='room unreachable'):
        live_band.round_once(tmp_path)


def test_room_monitor_preserves_owned_coding_bindings(tmp_path,monkeypatch):
    config=bindings()
    config['seats']['poet'].update(agent_id='p',role='identity_creator',purpose='poetry')
    config['seats']['teacher'].update(agent_id='t',role='identity_creator',purpose='teaching')
    (tmp_path/'bindings.json').write_text(json.dumps(config))
    monkeypatch.setattr(live_band.simulation,'ACTORS',[('poet','identity_creator','poetry'),('teacher','identity_creator','teaching')])
    calls=[]
    def command(args,**kwargs):
        calls.append(args)
        return [] if kwargs.get('parse') else ''
    monkeypatch.setattr(live_band,'band',command)
    live_band.provision(tmp_path)
    assert not any('attach' in command for command in calls)
    assert any('invite' in command for command in calls)
