import json
import subprocess
from experiments.creator_simulation import creative_dispatch as dispatcher


def test_dispatch_is_bounded_and_does_not_repeat_tasks(tmp_path,monkeypatch):
    config={'room_id':'room','seats':{actor:{'agent_id':actor} for actor,_ in dispatcher.TASKS}}
    calls=[]
    monkeypatch.setattr(dispatcher.subprocess,'run',lambda args,**kwargs:(calls.append(args) or subprocess.CompletedProcess(args,0,'sent','')))
    assert dispatcher.dispatch(tmp_path,config,limit=2)==2
    assert len(calls)==2
    assert dispatcher.dispatch(tmp_path,config,limit=1)==1
    assert len(calls)==3
    assert [x[-1] for x in calls]==['poet','newsletter','teacher']
    records=json.loads((tmp_path/'creative_tasks.json').read_text())
    assert all(r['status']=='dispatched' for r in records.values())
    assert all(r['status']!='completed' for r in records.values())
