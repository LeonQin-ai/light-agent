"""Actual Docker build/run isolation and persistence smoke test; no paid API."""
import json,subprocess,time,urllib.error,urllib.request,uuid
from pathlib import Path
root=Path(__file__).resolve().parents[2];tag='coa-smoke-'+uuid.uuid4().hex[:12]
def docker(*args):return subprocess.check_output(['docker',*args],cwd=root,text=True).strip()
volumes=[tag+'-state',tag+'-work'];container=None
try:
 subprocess.run(['docker','build','-t',tag,'.'],cwd=root,check=True)
 for volume in volumes:docker('volume','create',volume)
 container=docker('run','-d','--name',tag,'--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true','--pids-limit=256','--memory=2g','--cpus=2','--tmpfs','/tmp:rw,size=128m','-p','127.0.0.1::18300','-v',volumes[0]+':/state','-v',volumes[1]+':/workspace',tag)
 port=json.loads(docker('inspect',container))[0]['NetworkSettings']['Ports']['18300/tcp'][0]['HostPort']
 def auth_key():
  for _ in range(100):
   try:
    key=docker('exec',container,'python3','-c',"import json;print(json.load(open('/state/light-agent.json',encoding='utf-8')).get('auth.key',''))")
    if key:return key
   except subprocess.CalledProcessError:pass
   time.sleep(.2)
  raise AssertionError('container did not seed auth.key')
 key=auth_key()
 def wait():
  for _ in range(100):
   try:
    req=urllib.request.Request(f'http://127.0.0.1:{port}/v1/scheduler',headers={'Authorization':'Bearer '+key})
    with urllib.request.urlopen(req,timeout=2) as r:return json.load(r)
   except OSError:time.sleep(.2)
  raise AssertionError('container HTTP startup timeout')
 wait()
 try:
  urllib.request.urlopen(f'http://127.0.0.1:{port}/v1/scheduler',timeout=2)
  raise AssertionError('non-loopback container accepted an unauthenticated request')
 except urllib.error.HTTPError as e:assert e.code==401,e.code
 assert docker('exec',container,'id','-u')=='10001'
 inspection=json.loads(docker('inspect',container))[0]
 assert inspection['HostConfig']['ReadonlyRootfs'] and inspection['HostConfig']['CapDrop']==['ALL']
 docker('exec',container,'python3','-c',"from pathlib import Path;Path('/workspace/proof').write_text('persisted');Path('/state/proof').write_text('retained');assert not Path('/src').exists()")
 denied=subprocess.run(['docker','exec',container,'python3','-c',"open('/etc/coa-write-test','w').write('bad')"],capture_output=True)
 assert denied.returncode!=0,'root filesystem writable'
 docker('restart',container)
 # Docker may allocate a different ephemeral host port after restarting.
 port=json.loads(docker('inspect',container))[0]['NetworkSettings']['Ports']['18300/tcp'][0]['HostPort']
 assert auth_key()==key,'auth.key must persist across restart'
 wait()
 docker('exec',container,'python3','-c',"from pathlib import Path;assert Path('/workspace/proof').read_text()=='persisted';assert Path('/state/proof').read_text()=='retained'")
 print('PASS: non-root, read-only root, capabilities dropped, HTTP access, persistent state/workspace and restart')
finally:
 if container:
  print(docker('logs',container));docker('rm','-f',container)
 for volume in volumes:subprocess.run(['docker','volume','rm',volume],capture_output=True)
 subprocess.run(['docker','image','rm',tag],capture_output=True)
