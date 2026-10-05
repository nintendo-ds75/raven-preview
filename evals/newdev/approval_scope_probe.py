"""Diagnostic: mandatory path approval must survive task/parent inheritance
and multi-path input. Prints observed behavior; it is not a passing regression
test for the faulty behavior. Run with `python -m evals.newdev.approval_scope_probe`.
No model, network, shared database or production data.
"""
import json,os,sys,tempfile
from pathlib import Path
os.environ['BRIDGE_MODEL_API']='none';os.environ['BRIDGE_SEMANTIC']='0'
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bridge import canvas,mcp
from bridge.authz import Actor
from bridge.config import Config
from bridge.store import Store
out=[]
for mode in ('explicit_single','inherited_task','inherited_parent','explicit_multiple'):
 with tempfile.TemporaryDirectory() as temp:
  store=Store(Path(temp)/'p.db');g=store.graph;cfg=Config(model_api='none');repo='evaluation/approval'
  with g.transaction():
   primary=g.add_person('A Runtime Owner',email='runtime@scope.invalid')
   reviewer=g.add_person('Z Required Reviewer',email='reviewer@scope.invalid')
   g.add_authority('repo','','decides',person_id=primary,repo=repo,source='config',asserted_by='operator',accepted=True)
   g.add_authority('path','protected/*','approves',person_id=reviewer,repo=repo,source='config',asserted_by='operator',accepted=True)
  task=canvas.start_task(store,cfg,{'title':'Set compatibility policy','repo':repo,'paths':'protected/retry.py','client_key':mode})['task_id']
  args={'task_id':task,'question':'Should the new optional setting preserve current behavior by default?','client_ref':'child','category':'compat'}
  if mode=='explicit_single':args['paths']='protected/retry.py'
  if mode=='explicit_multiple':args['paths']='public/docs.rst, protected/retry.py'
  if mode=='inherited_parent':
   parent=canvas.add_node(store,cfg,{'task_id':task,'question':'Which API shape should be chosen for this option?','paths':'protected/retry.py','client_ref':'root','category':'compat'})
   store.answer(parent['node_id'],{'answer':'Use an optional numeric parameter.','rationale':'Preserve defaults.'},actor=Actor.person(g.get_person(primary)))
   d=store.get_decision(parent['node_id'])
   canvas.sign_off(store,parent['node_id'],{'expected_updated_at':d['updated_at']},actor=Actor.person(g.get_person(reviewer)))
   args['parent_id']=parent['node_id']
  node=canvas.add_node(store,cfg,args)
  store.answer(node['node_id'],{'answer':'Preserve all existing defaults.','rationale':'New behavior is opt-in.'},actor=Actor.person(g.get_person(primary)))
  tree=canvas.get_tree(store,task)
  try:finished=canvas.finish_task(store,{'task_id':task,'checks':'Diagnostic, no code changes.'})
  except Exception as e:finished={'error':str(e)}
  out.append({'mode':mode,'node':node,'tree':tree,'finish':finished})
print(json.dumps(out,indent=2))
