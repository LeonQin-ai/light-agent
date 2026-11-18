"""Exercise the chat scheduler, native tool, real OS keyring and a TLS API."""
import base64
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
import trustme

ROOT = Path(__file__).resolve().parents[1]


def run():
    secret = secrets.token_hex(24)
    ref = None
    calls = []
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith('/repo.git/'):
                valid=self.headers.get('Authorization')=='Basic '+base64.b64encode(('fixture:'+secret).encode()).decode()
                hits.append(valid)
                if not valid:
                    self.send_error(403);return
                repo=work/'repo.git'
                args=['git','upload-pack','--stateless-rpc','--advertise-refs',str(repo)]
                data=b'001e# service=git-upload-pack\n0000'+subprocess.check_output(args)
                self.send_response(200);self.send_header('Content-Type','application/x-git-upload-pack-advertisement')
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            valid = self.headers.get('Authorization') == 'Bearer ' + secret
            hits.append(valid)
            self.send_response(200 if valid else 403)
            self.end_headers();self.wfile.write(('verified '+secret).encode())

        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            if self.path=='/repo.git/git-upload-pack':
                valid=self.headers.get('Authorization')=='Basic '+base64.b64encode(('fixture:'+secret).encode()).decode()
                hits.append(valid)
                if not valid:self.send_error(403);return
                data=subprocess.check_output(['git','upload-pack','--stateless-rpc',str(work/'repo.git')],input=body)
                self.send_response(200);self.send_header('Content-Type','application/x-git-upload-pack-result')
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            assert secret.encode() not in body
            calls.append(body)
            name, args = ('credential_use', {'reference': ref}) if len(calls)==1 else (
                'complete_task', {'status':'complete','requires_tools':False,'answer':'Checked endpoint.'})
            message = {'role':'assistant','content':None,'tool_calls':[{'id':'call_'+str(len(calls)),
                'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}
            raw=json.dumps({'choices':[{'message':message,'finish_reason':'tool_calls'}]}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(raw)))
            self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)

    with tempfile.TemporaryDirectory() as folder:
        work=Path(folder)
        state=work/'state'; state.mkdir()
        provider=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        service=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        ca=trustme.CA(); ca.cert_pem.write_to_path(work/'ca.pem')
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ca.issue_cert('localhost').configure_cert(tls)
        service.socket=tls.wrap_socket(service.socket,server_side=True)
        for server in (service,provider):
            threading.Thread(target=server.serve_forever,daemon=True).start()
        (state/'light-agent.json').write_text(json.dumps({'llm.provider':'openai','llm.model':'fixture',
            'llm.base_url':f'http://127.0.0.1:{provider.server_port}','llm.api_key':'fixture'}))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        process=subprocess.Popen([str(ROOT/'build'/('light-agent.exe' if os.name=='nt' else 'light-agent')),
            '--state',str(state),'--workspace',str(work),'serve',str(port)],cwd=ROOT,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env={**os.environ,'COA_TOOL_PROTOCOL':'native',
            'PGSSLROOTCERT':str(work/'ca.pem'),'SSL_CERT_FILE':str(work/'ca.pem'),'LIGHT_AGENT_CREDENTIAL_PYTHON':sys.executable})
        def api(path,body=None,method=None):
            req=Request(f'http://127.0.0.1:{port}'+path,method=method,
                data=json.dumps(body).encode() if body else None,headers={'Content-Type':'application/json'})
            with urlopen(req,timeout=30) as response:return json.load(response)
        session='grant-test'
        try:
            for _ in range(100):
                try:api('/v1/scheduler');break
                except OSError:time.sleep(.1)
            ref=api('/v1/credential-grants',{'mode':'strict','session':session,'kind':'api',
                'target':f'https://localhost:{service.server_port}/check','secret':secret})['reference']
            def exercise(gate, sid):
                calls.clear();hits.clear()
                tid=api('/v1/chat',{'session':sid,'message':'Check the authorized endpoint using '+ref,
                    'gate':gate,'permission_mode':'full','workspace':str(work)})['id']
                end=time.monotonic()+45
                while time.monotonic()<end:
                    task=api('/v1/tasks/'+str(tid))
                    if task['status'] not in ('RUNNING','QUEUED'):break
                    time.sleep(.05)
                assert any(s['tool']=='credential_use' for s in task['steps']),task['status']
                assert secret not in json.dumps(task)
                return task
            task=exercise('full',session);assert hits==[True] and '[REDACTED]' in task['steps'][0]['out'],hits
            exercise('simple',session);assert not hits
            exercise('full','other-chat');assert not hits
            api('/v1/credential-grants',{'session':session,'reference':ref},'DELETE')
            exercise('full',session);assert not hits
            repo=work/'repo.git'
            subprocess.run(['git','init','-q',str(repo)],check=True)
            (repo/'proof.txt').write_text('credential clone verified')
            subprocess.run(['git','-C',str(repo),'add','proof.txt'],check=True)
            subprocess.run(['git','-C',str(repo),'-c','user.name=fixture','-c','user.email=test@example.invalid',
                            'commit','-qm','fixture'],check=True)
            for kind in ('git','clone'):
                ref=api('/v1/credential-grants',{'mode':'strict','session':session,'kind':kind,'user':'fixture',
                    'target':f'https://localhost:{service.server_port}/repo.git','secret':secret})['reference']
                task=exercise('full',session)
                assert task['steps'][0]['ok'],task['steps'][0].get('out')
                assert hits and all(hits),kind
                if kind=='clone':assert any(p.read_text()=='credential clone verified' for p in work.glob('repo-*/proof.txt'))
                api('/v1/credential-grants',{'session':session,'reference':ref},'DELETE')
            if os.environ.get('PG_TEST_BIN'):
                binaries=Path(os.environ['PG_TEST_BIN'])
                data=work/'postgres-data'
                pwfile=work/'init.pw';pwfile.write_text(secret)
                subprocess.run([str(binaries/'initdb'),'-D',str(data),'-U','fixture','--locale=C','--encoding=UTF8','-A','scram-sha-256',
                                '--pwfile',str(pwfile)],check=True,stdout=subprocess.DEVNULL)
                pwfile.unlink()
                cert=ca.issue_cert('localhost')
                cert.cert_chain_pems[0].write_to_path(data/'server.crt')
                cert.private_key_pem.write_to_path(data/'server.key')
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1',0));pgport=sock.getsockname()[1]
                ctl=[str(binaries/'pg_ctl'),'-D',str(data)]
                subprocess.run(ctl+['-l',str(work/'postgres.log'),'-o',f'-p {pgport} -h 127.0.0.1 -c ssl=on','-w','start'],
                               check=True,stdout=subprocess.DEVNULL)
                try:
                    ref=api('/v1/credential-grants',{'mode':'strict','session':session,'kind':'postgres','user':'fixture','query':'SELECT current_user, current_database()',
                        'target':f'postgresql://localhost:{pgport}/postgres','secret':secret})['reference']
                    task=exercise('full',session)
                    assert task['steps'][0]['ok'],task['steps'][0]['out']
                    assert 'fixture' in task['steps'][0]['out'] and 'postgres' in task['steps'][0]['out']
                    api('/v1/credential-grants',{'session':session,'reference':ref},'DELETE')
                    print('PASS: PostgreSQL TLS and SCRAM password authentication')
                finally:
                    subprocess.run(ctl+['-m','fast','-w','stop'],check=True,stdout=subprocess.DEVNULL)
            for file in state.rglob('*'):
                if file.is_file():assert secret.encode() not in file.read_bytes(),file.name
            print('PASS: grant, strict authenticated HTTPS, Git refs and clone, fast denial, session isolation, revocation and state scan')
        finally:
            if ref:
                try:api('/v1/credential-grants',{'session':session,'reference':ref},'DELETE')
                except OSError:pass
            process.terminate();process.wait(timeout=5)
            for server in (service,provider):server.shutdown();server.server_close()

if __name__=='__main__':run()
