"""The observer response stays bounded while accepting synthetic populations."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from isleworks.domain import ScenarioConfig, WorldState, canonical
from isleworks.interfaces.server import make_server
from isleworks.runner import Runner
from isleworks.storage import Store


class ObserverScaleTest(unittest.TestCase):
    def test_bounded_snapshot_and_resident_lookup(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'archive.sqlite3')
            runner=Runner(store)
            run_id=runner.create(ScenarioConfig(days=1),review_enabled=False)
            runner.advance(run_id)
            # Expand only a committed snapshot into synthetic visualization input;
            # this is not a 10,000-agent economic simulation.
            with store.connect() as db:
                row=db.execute('SELECT body,plans FROM days WHERE run_id=? AND day=1',(run_id,)).fetchone()
                body,plans=json.loads(row[0]),json.loads(row[1])
                originals=list(body['state']['residents'].values());original_plans=list(plans.values());intents=body['intents']
                population={};expanded_plans={};expanded_intents=[]
                for i in range(10000):
                    rid=f'synthetic-{i:05d}';resident=copy.deepcopy(originals[i%len(originals)]);resident['id']=rid;population[rid]=resident
                    p=copy.deepcopy(original_plans[i%len(original_plans)]);p['resident_id']=rid
                    for entry in p['schedule']:entry['resident_id']=rid
                    expanded_plans[rid]=p
                    intent=copy.deepcopy(intents[i%len(intents)]);intent['resident_id']=rid;expanded_intents.append(intent)
                body['state']['residents']=population;body['intents']=expanded_intents
                body['state_hash']=WorldState.from_dict(body['state']).state_hash
                db.execute('UPDATE days SET body=?,state_hash=?,plans=? WHERE run_id=? AND day=1',
                           (canonical(body),body['state_hash'],canonical(expanded_plans),run_id))
            server=make_server(store,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                def get(path):
                    c=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=15)
                    c.request('GET',path);r=c.getresponse();raw=r.read();c.close();self.assertEqual(r.status,200,raw);return json.loads(raw),len(raw)
                overview,size=get(f'/api/runs/{run_id}/observer?day=1')
                self.assertEqual(overview['population']['total'],10000)
                self.assertEqual(len(overview['state']['residents']),24)
                self.assertEqual(sum(overview['population']['counts'].values()),10000)
                self.assertLess(size,100000)
                self.assertNotIn('synthetic-09999',overview['state']['residents'])
                individual,_=get(f'/api/runs/{run_id}/residents/synthetic-09999?day=1')
                self.assertEqual(individual['resident']['id'],'synthetic-09999')
                self.assertEqual(individual['day'],1)
                historic,_=get(f'/api/runs/{run_id}/observer?day=0')
                self.assertIsNone(historic['facts_report']);self.assertIsNone(historic['llm_review'])
                self.assertEqual(store.calls(run_id),[])
            finally:
                server.shutdown();thread.join();server.server_close()


if __name__=='__main__': unittest.main()
