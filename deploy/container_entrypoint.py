"""Seed container defaults without embedding credentials or replacing user settings."""
import json,os,sys
from pathlib import Path
state=Path('/state');config=state/'light-agent.json'
source=config if config.exists() else state/'cognitive-os-agent.json'
data=json.loads(source.read_text(encoding='utf-8-sig')) if source.exists() else {}
# A container must listen on its virtual interface. Compose publishes loopback only.
data['http.bind']='0.0.0.0'
if 'llm.provider' not in data:data['llm.provider']='mock'
# Non-loopback serving requires a bearer key; seed one into the persisted
# state so a fresh container is never open by default.
if not data.get('auth.key'):
    import secrets
    data['auth.key']=secrets.token_hex(24)
os.umask(0o077)
temporary=state/'light-agent.json.tmp'
temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
os.replace(temporary,config)
os.execvp('light-agent',['light-agent','--state','/state','--workspace','/workspace',*sys.argv[1:]])
