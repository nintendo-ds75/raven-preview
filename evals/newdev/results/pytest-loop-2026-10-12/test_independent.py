"""External oracle: not mounted in the coding host until its work is complete."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

MARKER='[... output truncated ...]'
CODE='''import logging, sys

def test_output():
    print("αβγδεζηθ", end="")
    print("ERR_123456", file=sys.stderr, end="")
    logging.warning("LOG_123456")

def test_failure():
    print("failure_stdout_123456", end="")
    assert False, "FAILURE_DIAGNOSTIC_MUST_REMAIN_FULL_123456789"
'''

def run(limit=None, mode='all', passing=True, family='xunit2'):
    with tempfile.TemporaryDirectory() as t:
        p=Path(t);(p/'test_case.py').write_text(CODE)
        cmd=[sys.executable,'-m','pytest','-q',str(p/'test_case.py'),'--junitxml='+str(p/'report.xml'),'-o','junit_logging='+mode,'-o','junit_log_passing_tests='+str(passing).lower(),'-o','junit_family='+family,'-o','log_format=%(message)s']
        if limit is not None:cmd+=['-o','junit_output_limit='+str(limit)]
        r=subprocess.run(cmd,cwd=t,env={**os.environ,'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'},text=True,capture_output=True)
        return r,ET.parse(p/'report.xml').getroot() if (p/'report.xml').exists() else None

def main():
    checks=[]
    def check(name,ok):
        checks.append((name,bool(ok)));print(('PASS ' if ok else 'FAIL ')+name)
    for family in ['xunit1','xunit2']:
        r,x=run(5,family=family);check(family+' reports actual test failure',r.returncode==1 and x is not None)
        if x is None:continue
        cases={n.attrib['name']:n for n in x.iter('testcase')};n=cases['test_output'];out=n.findtext('system-out') or '';err=n.findtext('system-err') or ''
        check(family+' Unicode prefix uses code points','αβγδε' in out and 'ζηθ' not in out)
        check(family+' stdout and log budgets independent','LOG_1' in out and 'LOG_123456' not in out and out.count(MARKER)==2)
        check(family+' stderr budget independent','ERR_1' in err and 'ERR_123456' not in err and MARKER in err)
        check(family+' failure diagnostic intact','FAILURE_DIAGNOSTIC_MUST_REMAIN_FULL_123456789' in (cases['test_failure'].findtext('failure') or ''))
    for limit in [None,0,999]:
        r,x=run(limit);text=ET.tostring(x,encoding='unicode') if x is not None else ''
        check('unlimited or below threshold '+str(limit),'αβγδεζηθ' in text and MARKER not in text)
    r,x=run(5,passing=False);n=next(z for z in x.iter('testcase') if z.attrib['name']=='test_output')
    check('passing output filtering preserved',n.find('system-out') is None and n.find('system-err') is None)
    r,x=run(5,mode='no');check('logging disabled remains disabled',not list(x.iter('system-out')) and not list(x.iter('system-err')))
    for value in ['-1','1.5','garbage']:
        r,x=run(value);check('invalid configuration rejected '+value,r.returncode==4 and 'junit_output_limit' in r.stdout+r.stderr)
    print(f'{sum(ok for _,ok in checks)}/{len(checks)} independent checks passed')
    return int(not all(ok for _,ok in checks))
if __name__=='__main__':raise SystemExit(main())
