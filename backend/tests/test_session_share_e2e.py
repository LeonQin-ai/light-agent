"""Real Agent HTTP: session export/import sharing + per-turn rewind (memory travels)."""
import json, os, socket, subprocess, tempfile, threading, time, urllib.request, urllib.error
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
root=Path(__file__).resolve().parents[1];build=root/os.environ.get('COA_BUILD_DIR','build')
seen=[]
class Model(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        text='\n'.join(m['content'] for m in body['messages'] if isinstance(m.get('content'),str))
        seen.append(text)
        args={'status':'complete','requires_tools':False,'answer':'分享执行完成'}
        raw=json.dumps({'choices':[{'message':{'content':'','tool_calls':[{'id':'c','type':'function','function':{'name':'complete_task','arguments':json.dumps(args)}}]},'finish_reason':'tool_calls'}]}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
model=ThreadingHTTPServer(('127.0.0.1',0),Model);threading.Thread(target=model.serve_forever,daemon=True).start()
try:
 with tempfile.TemporaryDirectory(prefix='coa-share-') as tmp:
    base=Path(tmp).resolve();state=base/'state';state.mkdir();a=base/'proj';a.mkdir()
    (state/'light-agent.json').write_text(json.dumps({'llm.provider':'openai','llm.model':'fixture','llm.base_url':f'http://127.0.0.1:{model.server_port}','llm.api_key':'fixture','reasoning.max_rounds':8,'scheduler.workers':2}),encoding='utf-8')
    def new_port():
        with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
    port=new_port()
    def req(path,body=None):
        r=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(r,timeout=15) as response:return json.load(response)
    def err(path,body=None):
        try:req(path,body);raise AssertionError('expected HTTP error for '+path)
        except urllib.error.HTTPError as e:return e.code
    def chat_done(sid,message):
        tid=req('/v1/chat',{'message':message,'session':sid})['id']
        deadline=time.monotonic()+20
        while True:
            t=req('/v1/tasks/'+str(tid))
            if t['status'] not in ('RUNNING','QUEUED'):break
            assert time.monotonic()<deadline;time.sleep(.03)
        assert t['status']=='DONE',t.get('output')
        return t
    with (build/'session-share-e2e.log').open('w',encoding='utf-8') as log:
        p=subprocess.Popen([str(build/('light-agent.exe' if os.name=='nt' else 'light-agent')),'--state',str(state),'--workspace',str(base),'serve',str(port)],cwd=root,stdout=log,stderr=log,env={**os.environ,'COA_TOOL_PROTOCOL':'native'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            for _ in range(100):
                try:req('/v1/scheduler');break
                except OSError:assert p.poll() is None;time.sleep(.05)
            else:raise AssertionError('startup')
            req('/v1/workspaces',{'path':str(a)})
            sa=req('/v1/chat/sessions',{'workspace':str(a)})['id']
            req('/v1/chat/sessions/'+sa+'/title',{'title':'分享源会话'})
            chat_done(sa,'分享记忆第一问')
            chat_done(sa,'分享记忆第二问')
            # export: portable bundle, no local snapshot ids
            bundle=req('/v1/chat/sessions/'+sa+'/export',{})
            assert bundle['kind']=='coa-session' and bundle['version']==1
            assert bundle['title']=='分享源会话'
            assert len(bundle['turns'])==2
            assert bundle['turns'][0]['q']=='分享记忆第一问'
            assert all('snaps' not in t for t in bundle['turns'])
            # import: NEW session bound to the importer's workspace
            imp=dict(bundle);imp['workspace']=str(a)
            r=req('/v1/chat/sessions/import',imp)
            sb=r['id'];assert sb!=sa and r['turns']==2
            assert Path(r['workspace'])==a
            # continue executing with the imported memory: the planner prompt
            # must carry the imported turns
            seen.clear()
            chat_done(sb,'导入后继续执行')
            assert any('分享记忆第一问' in s and '分享记忆第二问' in s for s in seen),seen
            # rewind to before turn 1: keep turn 0, refill turn 1's question
            r=req('/v1/chat/sessions/'+sb+'/rewind',{'turn':1,'files':False})
            assert r['ok'] and r['turns']==1 and r['prompt']=='分享记忆第二问'
            seen.clear()
            chat_done(sb,'回滚后重发')
            assert any('分享记忆第一问' in s and '回滚后重发' in s for s in seen),seen
            assert not any('分享记忆第二问' in s for s in seen),'rewound turn must leave the context'
            # error surfaces
            assert err('/v1/chat/sessions/no-such/export',{})==404
            assert err('/v1/chat/sessions/no-such/rewind',{'turn':0})==404
            assert err('/v1/chat/sessions/'+sb+'/rewind',{'turn':99})==400
            assert err('/v1/chat/sessions/'+sb+'/rewind',{'turn':-1})==400
            assert err('/v1/chat/sessions/import',{'kind':'bad','version':1,'turns':[]})==400
        finally:p.terminate();p.wait(timeout=5)
    print('PASS: session export/import memory travel, continue execution, per-turn rewind truncation, error codes')
finally:
    def stop_model():
        model.shutdown();model.server_close()
    cleanup=threading.Thread(target=stop_model,daemon=True);cleanup.start();cleanup.join(5)
    if cleanup.is_alive():
        import faulthandler;faulthandler.dump_traceback()
        raise AssertionError('Share provider fixture teardown exceeded 5 seconds')