import json,subprocess,time,sys
from pathlib import Path
r=Path('<E2E_ROOT>'); label=sys.argv[1]
code="import importlib.util,json; s=importlib.util.spec_from_file_location('raven_installed','/workspace/.raven/host.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); c=json.load(open('/workspace/.raven/host-config.json')); print(json.dumps(m.watch_once(c,'claude','977e6834-3b21-4ad9-a358-17a6b88e05b4','pytest-real-supervisor')))"
t=time.time()
with (r/(label+'.json')).open('w') as f,(r/(label+'.stderr')).open('w') as err:
 p=subprocess.run(['docker','exec','raven-pytest-host','python3','-c',code],stdout=f,stderr=err,timeout=1250)
(r/(label+'.receipt.json')).write_text(json.dumps({'exit':p.returncode,'seconds':time.time()-t}))
print('Supervisor completed',p.returncode)
