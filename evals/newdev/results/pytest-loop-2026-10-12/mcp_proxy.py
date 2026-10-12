import http.client,json,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_POST(self):
  raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
  conn=http.client.HTTPConnection('raven-pytest-app-1',7333,timeout=1200)
  h={k:v for k,v in self.headers.items() if k.lower() not in ('host','connection')}
  h['Host']='localhost:17530';start=time.time();conn.request('POST',self.path,raw,h);r=conn.getresponse()
  self.send_response(r.status);self.send_header('Content-Type',r.getheader('Content-Type','application/json'));self.send_header('Connection','close');self.end_headers();self.close_connection=True
  chunks=[]
  try:
   while True:
    chunk=r.read1(65536)
    if not chunk:break
    chunks.append(chunk);self.wfile.write(chunk);self.wfile.flush()
  finally:
   conn.close();data=b''.join(chunks)
   try:
    req=json.loads(raw)
    if r.getheader('Content-Type','').startswith('text/event-stream'):
     events=[json.loads(line[6:]) for line in data.decode().splitlines() if line.startswith('data: ')]
     res=next((e for e in reversed(events) if 'result' in e or 'error' in e),{})
    else:events=[];res=json.loads(data)
    print(json.dumps({'trace':'mcp','at':start,'seconds':time.time()-start,'request':req,'status':r.status,'response':res,'stream':events}),flush=True)
   except ValueError:pass
ThreadingHTTPServer(('127.0.0.1',7333),Handler).serve_forever()
