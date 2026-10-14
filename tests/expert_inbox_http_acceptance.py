"""Actual local HTTP acceptance with synthetic accounts; no browser or provider claim."""
import http.client
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlencode, urlsplit

ROOT=Path(__file__).resolve().parents[1]
checks=[]
SOURCE_TREE_DIRTY=None
try:
    SOURCE_COMMIT=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','--verify','HEAD'],text=True,stderr=subprocess.DEVNULL).strip()
    SOURCE_TREE_DIRTY=bool(subprocess.check_output(['git','-C',str(ROOT),'status','--porcelain','--untracked-files=no'],text=True,stderr=subprocess.DEVNULL).strip())
except (OSError,subprocess.CalledProcessError):
    SOURCE_COMMIT='unknown (archive checkout without Git metadata)'

def request(base,path,method='GET',data=None,cookie='',csrf='',form=False,headers=None):
    u=urlsplit(base);conn=http.client.HTTPConnection(u.hostname,u.port,timeout=10)
    hs={'Cookie':cookie} if cookie else {}
    if data is not None:
        body=urlencode(data) if form else json.dumps(data)
        hs['Content-Type']='application/x-www-form-urlencoded' if form else 'application/json'
    else: body=None
    if csrf: hs['X-Bridge-CSRF']=csrf
    hs.update(headers or {})
    conn.request(method,path,body,hs);res=conn.getresponse();content=res.read().decode();out=(res.status,dict(res.getheaders()),content);conn.close();return out

def must(ok,why):
    if not ok: raise AssertionError(why)

def passed(name):
    checks.append(name);print('PASS '+name,flush=True)

def form(base,path,data):
    status,_,html=request(base,path)
    must(status==200,'Load form '+path)
    data={**data,'csrf':re.search(r'name="csrf" value="([^"]+)"',html)[1]}
    return request(base,path,'POST',data,form=True)

def login(base,email,password):
    status,_,html=request(base,'/auth/login');must(status==200,'Login form')
    csrf=re.search(r'name="csrf" value="([^"]+)"',html)[1]
    status,h,_=request(base,'/auth/password','POST',{'email':email,'password':password,'csrf':csrf},form=True)
    must(status==302,'Login redirect');must(h['Location']=='/#inbox','Login destination')
    cookie=h['Set-Cookie'].split(';')[0]
    status,_,body=request(base,'/api/state',cookie=cookie)
    must(status==200,'Login authenticated')
    return cookie,json.loads(body)

def main():
    with tempfile.TemporaryDirectory(prefix='raven-expert-http-') as d:
        proc=subprocess.Popen([sys.executable,str(ROOT/'tests/expert_inbox_browser_server.py'),d],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        try:
            w=json.loads(proc.stdout.readline());a,b=w['A'],w['B'];base=a['base']
            def ctl(action,**kwargs): return json.loads(request(w['control'],'/?'+urlencode({'action':action,**kwargs}))[2])
            def get(path,cookie=''): return request(base,path,cookie=cookie)
            def api(path,data,cookie,state): return request(base,path,'POST',data,cookie,state['csrf_token'])
            for path in ('/api/state','/api/inbox','/api/tasks/'+a['task']+'/tree','/api/decisions/'+a['node']):
                s,_,body=get(path);must(s==401,path);must('SYNTHETIC_TASK_CONTEXT' not in body and 'TENANT_A_PRIVATE_GOAL' not in body,'Visitor leaked')
            s,h,body=get('/');must(s==302 and h['Location']=='/auth/login','Anonymous app login boundary')
            passed('Unauthenticated visitors receive no task, inbox, decision or source context')
            s,h,body=form(base,'/auth/join',{'invite':a['expired'],'name':'Synthetic expired','password':a['password']})
            must(s==400 and 'invalid or expired' in body,'Expired invite');must('Set-Cookie' not in h,'Expired invite session')
            passed('Expired invitation refuses onboarding')
            s,h,body=form(base,'/auth/join',{'invite':a['invite'],'name':'Imposter display name','password':a['password']})
            must(s==302 and h['Location']=='/#inbox','Invite lands inbox');cookie=h['Set-Cookie'].split(';')[0]
            s,_,body=get('/api/state',cookie);state=json.loads(body);must(s==200,'Invite authenticated');must(state['me']['id']==a['expert']['id'] and state['me']['name']==a['expert']['name'],'Mapped person identity');must(state['me']['role']=='member','Invite role')
            s,_,body=get('/api/inbox',cookie);must(s==200 and a['node'] in body,'Expert decision in inbox')
            s,_,body=form(base,'/auth/join',{'invite':a['invite'],'name':'Replay','password':a['password']});must(s==400,'Invite replay rejected')
            passed('Invitation creates login for existing expert identity and preserves authority; replay refused')
            cookie,state=login(base,a['expert']['email'],a['password'])
            s,_,body=get('/api/tasks/'+a['task']+'/tree',cookie);must(s==200 and 'TENANT_A_PRIVATE_GOAL' in body,'Task context')
            s,_,body=get('/api/decisions/'+a['node'],cookie);decision=json.loads(body);must(s==200 and 'SYNTHETIC_TASK_CONTEXT' in body,'Decision context');must('sign' in decision['allowed_actions'],'Owner standing')
            passed('Returning expert login reads task and exact decision context with signoff standing')
            before=ctl('decision');counts=ctl('counts')
            s,_,body=api('/api/tasks/'+a['task']+'/inquiry',{'question':'Which context and production constraints apply?','focus_id':a['node']},cookie,state)
            result=json.loads(body);must(s==200 and not result['authorized'],'Inquiry read-only');must('SYNTHETIC_TASK_CONTEXT' in body,'Task evidence present');must('UNRELATED_DECISION_CANARY' not in body and 'UNRELATED_TASK_CANARY' not in body,'Inquiry task isolation');must(ctl('counts')==counts,'Inquiry created entities');must(ctl('decision')['updated_at']==before['updated_at'],'Inquiry changed decision')
            passed('Read-only inquiry returns task evidence, excludes unrelated task and creates no authority')
            for data in ({'question':'Read other decision','focus_id':a['other_node']},{'question':'Read tenant B','task_id':b['task']},{'question':'Approve','signoff':True},{'question':'Approve','answer':'Yes'}):
                s,_,body=api('/api/tasks/'+a['task']+'/inquiry',data,cookie,state);must(s==400 and 'UNRELATED_DECISION_CANARY' not in body,'Forged inquiry fields')
            passed('Forged inquiry task, focus, answer and signoff fields fail closed')
            peer_cookie,peer_state=login(base,a['peer']['email'],a['password'])
            s,_,body=get('/api/decisions/'+a['node'],peer_cookie);peer_decision=json.loads(body);must(s==200,'Shared workspace read');must('sign' not in peer_decision['allowed_actions'],'Wrong person standing')
            s,_,body=api('/api/decisions/'+a['node']+'/signoff',{'expected_updated_at':decision['updated_at'],'signed_by':a['expert']['name'],'by':a['expert']['name']},peer_cookie,peer_state)
            must(s==403 and not ctl('decision')['authorized'],'Wrong-person signoff')
            viewer_cookie,viewer_state=login(base,a['viewer']['email'],a['password'])
            s,_,body=api('/api/tasks/'+a['task']+'/inquiry',{'question':'What is still pending?'},viewer_cookie,viewer_state);must(s==200 and not json.loads(body)['authorized'],'Viewer inquiry')
            s,_,body=api('/api/decisions/'+a['node']+'/signoff',{'expected_updated_at':decision['updated_at']},viewer_cookie,viewer_state);must(s==403,'Viewer signoff')
            passed('Other member cannot spoof expert signature; viewer can inquire but cannot sign')
            s,_,body=api('/api/decisions/'+a['node']+'/signoff',{'expected_updated_at':decision['updated_at']},cookie,state)
            must(s==200,'Owner signoff: '+body);decision=ctl('decision');must(decision['authorized'] and decision['signed_by']==a['expert']['name'],'Owner authorization')
            events=[json.loads(e['detail']) for e in decision['events'] if e['kind']=='signoff']
            must(any(e.get('actor_id')==a['expert']['id'] and e.get('actor_kind')=='session' for e in events),'Stable session provenance')
            passed('Expert signoff authorizes reviewed answer with stable authenticated identity provenance')
            s,_,body=request(b['base'],'/api/state',cookie=cookie);must(s==401 and 'TENANT_B_PRIVATE_GOAL' not in body,'Cross-instance session')
            s,_,body=form(b['base'],'/auth/join',{'invite':a['invite'],'name':'Cross tenant attempt','password':a['password']});must(s==400 and 'invalid or expired' in body,'Cross-instance invitation')
            s,_,body=get('/api/tasks/'+b['task']+'/tree',cookie);must(s in (400,404),'Foreign task IDs')
            passed('Independent organization instance rejects foreign session, invite and task identifiers')
            s,_,body=get('/api/decisions/'+a['restricted_node'],cookie);must(s==200 and 'REVOKED_SOURCE_CANARY' in body,'Source initially current');ctl('revoke_source')
            for path in ('/api/state','/api/tasks/'+a['restricted_task']+'/tree','/api/decisions/'+a['restricted_node']):
                s,_,body=get(path,cookie);must('REVOKED_SOURCE_CANARY' not in body,'Source leaked from '+path)
            s,_,body=api('/api/tasks/'+a['restricted_task']+'/inquiry',{'question':'What allowance applies?'},cookie,state);must(s==400 and 'REVOKED_SOURCE_CANARY' not in body,'Revoked source inquiry')
            passed('Source revocation withholds copied decision, task, state and inquiry content')
            s,_,body=request(base,'/api/brief',headers={'X-Raven-Link':a['expired_legacy']});must(s==401 and 'SYNTHETIC_TASK_CONTEXT' not in body,'Expired legacy link')
            ctl('revoke_expert')
            for path in ('/api/state','/api/inbox','/api/tasks/'+a['task']+'/tree','/api/decisions/'+a['node']):
                s,_,body=get(path,cookie);must(s==401 and 'SYNTHETIC_TASK_CONTEXT' not in body,'Inactive identity: '+path)
            s,_,body=api('/api/tasks/'+a['task']+'/inquiry',{'question':'Read after identity revoked'},cookie,state);must(s==401,'Inactive inquiry')
            s,_,body=request(base,'/api/brief',headers={'X-Raven-Link':a['legacy']});must(s==401,'Inactive legacy bearer')
            passed('Expired legacy link and revoked expert identity cannot read via session or legacy link')
            result={'ok':True,'commit':SOURCE_COMMIT,'source_tree_dirty':SOURCE_TREE_DIRTY,'checks':checks,'scope':'Actual loopback HTTP, synthetic identities, independent temporary stores; no real GitHub OAuth, inference or external notifications.','browser':'Not executed by this HTTP-only script; see the separate browser acceptance results.'}
            target=ROOT/'test-results/expert-inbox-http-result.json';target.parent.mkdir(exist_ok=True);target.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
        finally:
            proc.terminate();proc.wait(timeout=10)
if __name__=='__main__':main()
