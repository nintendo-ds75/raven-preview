"""The actual stdio proxy records HTTP calls without exposing its credential."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from evals.audit.ledger import Ledger


class ProxyTests(unittest.TestCase):
    def test_actual_stdio_http_roundtrip_and_transport_error_are_audited(self):
        credential='synthetic-proxy-credential'
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                requests.append(self.headers.get('Authorization'))
                msg=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if msg['id']==2:
                    self.send_response(503);self.end_headers();return
                body=json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'content':[{'type':'text','text':json.dumps({'node_id':'one','question':'Which format?', 'token':credential})}]}}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);trace=root/'trace.jsonl';ledger=root/'audit.jsonl'
                env={**os.environ,'BRIDGE_TOKEN':credential,'BRIDGE_EVAL_URL':f'http://127.0.0.1:{server.server_port}',
                     'BRIDGE_EVAL_TRACE':str(trace),'BRIDGE_EVAL_AUDIT':str(ledger)}
                lines='\n'.join(json.dumps({'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':'bridge_get_tree','arguments':{'task_id':'one'}}}) for i in (1,2))+'\n'
                proc=subprocess.run([sys.executable,'-m','evals.real_oss_remote.mcp_proxy'],input=lines,text=True,capture_output=True,env=env,timeout=15)
                self.assertEqual(proc.returncode,0,proc.stderr)
                replies=[json.loads(line) for line in proc.stdout.splitlines()]
                self.assertEqual([r['id'] for r in replies],[1,2])
                self.assertIn('error',replies[1])
                self.assertEqual(requests,['Bearer '+credential]*2)
                # Host receives the genuine response; only stored trace is redacted.
                self.assertIn(credential,proc.stdout)
                self.assertNotIn(credential,trace.read_text());self.assertNotIn(credential,ledger.read_text())
                rows=Ledger(ledger).read();self.assertEqual(len(rows),4)
                self.assertEqual(len({r['session'] for r in rows}),1)
        finally:
            server.shutdown();server.server_close();t.join()


if __name__=='__main__':unittest.main()
