import json,subprocess,sys,time
from pathlib import Path
root=Path('<E2E_ROOT>')
label=sys.argv[1]
brief=(root/'sealed/brief.txt').read_text()
instructions=(root/'evidence/host-instructions.txt').read_text()
command=['docker','exec','-i','raven-pytest-host','claude','-p','--model','claude-sonnet-5','--permission-mode','auto','--output-format','stream-json','--verbose','--max-turns','70','--max-budget-usd','15','--append-system-prompt',instructions,'--disallowedTools','Agent,WebFetch,WebSearch']
started=time.time()
with (root/(label+'.jsonl')).open('w') as out,(root/(label+'.stderr')).open('w') as err:
 result=subprocess.run(command,input=brief,text=True,stdout=out,stderr=err,timeout=1800)
(root/(label+'.receipt.json')).write_text(json.dumps({'exit':result.returncode,'seconds':time.time()-started}))
print('Claude phase completed',result.returncode)
