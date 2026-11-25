"""#236: task prose never substitutes for structured decisions or receipts."""
import base64, json, os, socket, subprocess, tempfile, threading, time, urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from fixture_http_response import respond_json

ROOT=Path(__file__).resolve().parents[1]
BUILD=ROOT/os.environ.get('COA_BUILD_DIR','build')
BINARY=BUILD/('light-agent.exe' if os.name=='nt' else 'light-agent')
CONTENT='ACTUAL_ARTIFACT_PROOF'
FINAL='I will read is a quoted phrase. 已完成；检查依据在实际执行记录中。'
def action(name,**args):return {'tool':name,'args':args}
def finish(**extra):return action('complete_task',status='complete',requires_tools=False,answer=FINAL,acceptance={},**extra)
def declare(**acceptance):return action('declare_task',acceptance=acceptance)

def exercise(protocol,mode,prompt='Verify the requested result.',live_image_route=None):
    calls=0;acceptance=None
    declaration_requests=[]
    image_calls=[]
    png64='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
    write=action('file_write',path='report.any',content=CONTENT)
    read=action('file_read',path='report.any')
    done=finish()
    if mode=='plain_final':replies=[FINAL];expected='DONE'
    elif mode=='chat':replies=[done];expected='DONE'
    elif mode=='prefixed':replies=['我先检查文件。<tool_call>file_read{\"path\":\"tiny\"}',done];expected='DONE'
    elif mode=='quoted_commentary':replies=['Example: `<tool_call>shell{} `. Next I will read.',action('file_read',path='tiny'),done];expected='DONE'
    elif mode=='commentary':replies=['Changing wording does not finish a task.','I will inspect the file.',action('file_read',path='tiny'),done];expected='DONE'
    elif mode=='stuck':replies=['准备继续。','The next phase is still pending.','还需要继续查看。'];expected='FAILED'
    elif mode.startswith('declaration_'):
        contract=declare(requires_action=True,requires_observation=True,artifacts=['report.any'])
        if mode=='declaration_recovery':
            replies=[contract,contract,write,read,done];expected='DONE'
        else:
            replies=[contract];expected='FAILED'
    elif mode.startswith('report_scope_'):
        html = 'report.html'
        html_write = action('file_write', path=html, content='<html>Verified report</html>')
        html_read = action('file_read', path=html)
        complete = action('complete_task', status='complete', requires_tools=False, answer=FINAL)
        prompt = '整理一个 HTML 报告'
        expected = 'DONE'
        if mode == 'report_scope_recovery':
            replies = [declare(artifacts=[html], full_reads=['tiny']), action('file_read', path='tiny'),
                       declare(artifacts=['wrong.md']), html_write, html_read, complete]
        elif mode in ('report_scope_interleaved', 'report_scope_interleaved_stop'):
            prompt = '分析一下沙箱的代码流程，并输出一个html分析报告'
            replies = [declare(artifacts=['sandbox_analysis_report.html']), action('file_read', path='tiny'),
                       declare(artifacts=[html], full_reads=['tiny']), action('file_read', path='long.dat', max_bytes=32),
                       declare(artifacts=['sandbox-code-flow-report.html']), action('list_dir', path='.')]
            if mode == 'report_scope_interleaved_stop':
                replies += [declare(artifacts=['sandbox-analysis.html'])]
                expected = 'FAILED'
            else:
                replies += [html_write, html_read, complete]
        elif mode == 'report_scope_undeclared':
            prompt = '请输出一份 HTML 格式的分析报告'
            replies = [complete, html_write, html_read, complete]
        elif mode == 'report_scope_no_output':
            prompt = '分析一下沙箱的代码流程，并输出一个html分析报告'
            replies = [complete]
            expected = 'FAILED'
        elif mode == 'report_scope_fixed':
            acceptance = {'artifacts': [html]}
            replies = [declare(artifacts=['redirect.html']), html_write, html_read, complete]
        elif mode == 'report_scope_written':
            replies = [declare(artifacts=[html]), html_write,
                       declare(artifacts=['redirect.html']), html_read, complete]
        elif mode == 'report_scope_multiple':
            prompt = 'Create an HTML report and a Markdown appendix.'
            replies = [declare(artifacts=[html]), html_write, html_read,
                       declare(artifacts=['appendix.md']), action('file_write', path='appendix.md', content=CONTENT),
                       action('file_read', path='appendix.md'), done]
        else:
            raise ValueError(mode)
    elif mode in ('artifact','missing','erase','preexisting','write_only','stale'):
        replies=[declare(artifacts=['report.any'])]
        if mode in ('artifact','write_only','stale'):replies += [write]
        if mode in ('artifact','preexisting','stale'):replies += [read]
        if mode=='stale':replies += [action('file_write',path='report.any',content=CONTENT+' changed')]
        replies += [done];expected='DONE' if mode=='artifact' else 'FAILED'
    elif mode in ('edit','edit_failed','edit_stale'):
        replies=[declare(artifacts=['report.any']),action('file_edit',path='report.any',old_string='OLD_ARTIFACT' if mode!='edit_failed' else 'ABSENT',new_string=CONTENT),read]
        if mode=='edit_stale':replies += [action('file_edit',path='report.any',old_string=CONTENT,new_string='LATER_VERSION')]
        replies += [done];expected='DONE' if mode=='edit' else 'FAILED'
    elif mode in ('full','partial','small','optional'):
        path='tiny' if mode=='small' else 'long.dat'
        if mode!='optional':acceptance={'full_reads':[path]}
        replies=[action('file_read',path=path)]
        if mode=='full':replies += [action('file_read',path=path,offset_bytes=4000),action('file_read',path=path,offset_bytes=8000)]
        replies += [done];expected='FAILED' if mode=='partial' else 'DONE'
    elif mode=='api_missing':acceptance={'artifacts':['report.any']};replies=[done];expected='FAILED'
    elif mode=='artifact_recovery':
        acceptance={'artifacts':['report.any']};replies=[read,done,write,read,done];expected='DONE'
    elif mode=='image_history':
        replies=[action('file_read',path='preview.png'),done];expected='DONE'
    elif mode=='ui_artwork':
        acceptance={'artifacts':['assets/hero.png','index.html']}
        markup='<h1>Tea house</h1><img src="assets/hero.png" alt="Tea on a mountain terrace">'
        replies=[action('image_generate',prompt='Tea on a mountain terrace at dawn',path='assets/hero.png'),
                 action('file_write',path='index.html',content=markup),
                 action('file_read',path='index.html'),done]
        expected='DONE'
        prompt='Build a tea house landing page with an original generated hero image.'
    elif mode in ('rollover','rollover_stale'):
        replies=[declare(artifacts=['report.any']),write,read]
        replies += [[action('file_write',path=f'noise-{i}.txt',content=str(i)) for i in range(start,start+32)] for start in range(0,288,32)]
        if mode=='rollover_stale':replies += [action('file_write',path='report.any',content='changed after verification')]
        replies += [done];expected='DONE' if mode=='rollover' else 'FAILED'
    elif mode=='batch50':
        outputs=[f'chapter{i:02d}.md' for i in range(1,51)]
        replies=[declare(artifacts=outputs),action('shell',command='python produce50.py',outputs=outputs)]
        replies += [[action('file_read',path=p) for p in outputs[i:i+8]] for i in range(0,50,8)]
        replies += [done];expected='DONE'
    elif mode in ('script_output','script_reuse'):
        # Shell-declared artifact: the command writes the file and declares it
        # in outputs[]; the version-matched read back must satisfy acceptance.
        replies=[declare(artifacts=['report.any']),
                 action('shell',command="printf 'ACTUAL_ARTIFACT_PROOF' > report.any",outputs=['report.any']),
                 read,done];expected='DONE'
    elif mode in ('script_late_contract','script_windows_case'):
        filename='Évidence.any'
        target=filename.lower() if mode=='script_windows_case' else filename
        producer=action('shell',command='python produce.py',outputs=[filename])
        read_script=action('file_read',path=target)
        if mode=='script_windows_case':
            acceptance={'artifacts':[target],'full_reads':[target]}
            replies=[producer,read_script,done]
        else:
            replies=[producer,read_script,declare(artifacts=[target]),done]
        expected='DONE'
    elif mode in ('script_verified_edit','script_unverified_edit'):
        replacement=CONTENT if mode=='script_verified_edit' else 'NOT_PRESENT'
        replies=[action('file_write',path='report.any',content='OLD_RENDER'),
                 action('file_edit',path='report.any',old_string='ABSENT',new_string=replacement),
                 action('shell',command='python produce.py',outputs=['report.any']),read,done]
        expected='DONE' if mode=='script_verified_edit' else 'FAILED'
    elif mode=='script_budget_readback':
        acceptance={'artifacts':['report.any']}
        replies=[write,read,done,read,done];expected='DONE'
    elif mode=='script_unchanged':
        # Declaring outputs on a read-only command must not fake a write: the
        # file never changed after the obligation was merged, so the gate holds.
        replies=[declare(artifacts=['report.any']),
                 action('shell',command='cat report.any',outputs=['report.any']),
                 read,done];expected='FAILED'
    elif mode=='script_rewrite':
        replies=[declare(artifacts=['report.any']),write,
                 action('shell',command="printf 'SCRIPT_REPLACED_CONTENT' > report.any",outputs=['report.any']),read,done];expected='DONE'
    elif mode=='unreceipted_rewrite':
        replies=[declare(artifacts=['report.any']),write,
                 action('shell',command="printf 'UNRECEIPTED_VERSION' > report.any"),read,done];expected='FAILED'
    elif mode=='late_output_declaration':
        replies=[declare(artifacts=['report.any']),
                 action('shell',command="printf 'UNRECEIPTED_VERSION' > report.any"),read,
                 action('shell',command='cat report.any',outputs=['report.any']),done];expected='FAILED'
    elif mode=='nonproducing_output_declaration':
        replies=[declare(artifacts=['report.any']),
                 action('shell',command="printf 'UNRECEIPTED_VERSION' > report.any"),
                 action('shell',command='cat report.any',outputs=['report.any']),read,done];expected='FAILED'
    elif mode=='baseline_capacity':
        sources=[f'source-{i}.txt' for i in range(64)]
        replies=[declare(full_reads=sources,artifacts=['report.any'])]
        replies += [[action('file_read',path=path) for path in sources[i:i+8]] for i in range(0,64,8)]
        replies += [action('shell',command='cat report.any',outputs=['report.any']),read,done];expected='FAILED'
    elif mode in ('cache_shell_branch','cache_git_tag','cache_shell_env'):
        probe=action('grep',path='cache-target',pattern='CACHE_PROOF')
        if mode=='cache_shell_env':
            change=action('shell',command='env python mutate.py')
        else:
            name='branch' if mode=='cache_shell_branch' else 'tag'
            change=action('shell',command='git branch review-proof') if name=='branch' else action('git',args='tag review-proof')
            probe=action('glob',path='.git',pattern='**/review-proof')
        replies=[probe,change,probe,done];expected='DONE'
    elif mode in ('cycle_complete','cycle_missing'):
        replies=[action('file_read',path='tiny'),action('file_read',path='long.dat',max_bytes=16)]
        replies*=9
        if mode=='cycle_missing':acceptance={'artifacts':['missing.txt']}
        expected='DONE' if mode=='cycle_complete' else 'FAILED'
    elif mode=='observation':acceptance={'requires_observation':True};replies=[done];expected='FAILED'
    elif mode=='mutation':acceptance={'requires_action':True};replies=[action('file_read',path='tiny'),done];expected='FAILED'
    elif mode=='failed_tool':replies=[action('file_read',path='missing'),done];expected='FAILED'
    else:raise ValueError(mode)
    class Provider(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            nonlocal calls
            request=self.rfile.read(int(self.headers['Content-Length']))
            if mode=='ui_artwork' and self.path=='/images/generations':
                image_calls.append(json.loads(request))
                assert self.headers.get('Authorization')=='Bearer art-fixture'
                respond_json(self,json.dumps({'data':[{'b64_json':png64}]}).encode())
                return
            if mode=='ui_artwork' and calls==0:
                catalog=[t['function']['name'] for t in json.loads(request)['tools']]
                assert 'image_generate' in catalog and b'actual UI' in request
            if b'Summarize these tool observations' in request:
                # Observation compaction is a maintenance call: answer with a
                # valid summary without consuming a scripted planning reply.
                summary=json.dumps({'facts':['scripted fixture actions in progress'],'failures':[],'artifacts':[],'pending':['remaining steps']})
                respond_json(self,json.dumps({'choices':[{'message':{'content':summary},'finish_reason':'stop'}]}).encode())
                return
            if mode.startswith('declaration_'):
                declaration_requests.append(request)
            if (mode=='report_scope_recovery' and calls==3) or (mode.startswith('report_scope_interleaved') and calls==5):
                formatted = json.loads(request)
                context = formatted['messages'][-1]['content']
                contract = context.split('## Runtime acceptance contract\n', 1)[1].splitlines()[0]
                required = json.loads(contract)
                assert [Path(path).name for path in required['artifacts']] == ['report.html'], required
                assert [Path(path).name for path in required['full_reads']] == ['tiny'], required
                assert 'Contract declaration is unavailable' in formatted['messages'][0]['content']
                if protocol=='native':
                    assert 'declare_task' not in [tool['function']['name'] for tool in formatted['tools']]
                if mode=='report_scope_recovery':
                    assert 'Declare one artifact with the requested file extension' in context
                else:
                    assert '本次声明未合并，验收合同已锁定' in context
            if mode=='report_scope_undeclared' and calls==1:
                assert b'no verified write in this run' in request, request[-2000:]
            if mode=='script_late_contract' and calls==3:
                assert b'Previous completion was rejected:' not in request
            if mode=='script_budget_readback' and calls==4:
                assert b'Finalization-only reserve' in request
            if mode=='artifact_recovery' and calls==2:
                assert b'Previous completion was rejected:' in request and b'report.any' in request
            if mode.startswith('cycle_') and b'Cycle completion review:' in request:
                assert b'TINY_SOURCE_PROOF' in request
                reply=done
            else:reply=replies[min(calls,len(replies)-1)]
            calls+=1
            actions=reply if isinstance(reply,list) else [reply]
            message={'content':reply if isinstance(reply,str) else json.dumps(actions,ensure_ascii=False)}
            if protocol=='native' and not isinstance(reply,str):
                message={'content':'','tool_calls':[{'id':f'call-{calls}-{i}','type':'function','function':{'name':item['tool'],'arguments':json.dumps(item['args'])}} for i,item in enumerate(actions)]}
            respond_json(self,json.dumps({'choices':[{'message':message,'finish_reason':'stop'}]}).encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='coa-contract-') as folder:
            work=Path(folder);(work/'tiny').write_text('TINY_SOURCE_PROOF',encoding='utf8');(work/'long.dat').write_text('x'*9001,encoding='utf8')
            if mode=='image_history':
                png=base64.b64decode(png64)
                (work/'preview.png').write_bytes(png)
            if mode.startswith('cache_'):
                if mode=='cache_shell_env':
                    (work/'cache-target').write_text('before',encoding='utf8')
                    (work/'mutate.py').write_text("open('cache-target','w').write('CACHE_PROOF')",encoding='utf8')
                else:
                    subprocess.run(['git','init','-q',str(work)],check=True)
                    subprocess.run(['git','-C',str(work),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false','commit','--allow-empty','-qm','fixture'],check=True)
            if mode in ('edit','edit_failed','edit_stale','preexisting','stale','script_unchanged','baseline_capacity','artifact_recovery'):(work/'report.any').write_text('OLD_ARTIFACT',encoding='utf8')
            if mode=='baseline_capacity':
                for path in sources:(work/path).write_text('SOURCE',encoding='utf8')
            if mode in ('script_late_contract','script_windows_case'):
                (work/'produce.py').write_text("from pathlib import Path\nPath('Évidence.any').write_text('REAL_OUTPUT',encoding='utf8')",encoding='utf8')
            if mode in ('script_verified_edit','script_unverified_edit'):
                (work/'produce.py').write_text(f"from pathlib import Path\nPath('report.any').write_text({CONTENT!r},encoding='utf8')",encoding='utf8')
            if mode=='batch50':
                (work/'produce50.py').write_text("from pathlib import Path\nfor i in range(1,51): Path(f'chapter{i:02d}.md').write_text(f'CHAPTER {i} VERIFIED CONTENT',encoding='utf8')\n",encoding='utf8')
            max_rounds=1 if mode=='declaration_budget' else 4 if mode=='script_budget_readback' else 16
            (work/'light-agent.json').write_text(json.dumps({'llm.provider':'openai','llm.model':'contract-fixture','llm.api_key':'fixture','llm.base_url':f'http://127.0.0.1:{server.server_port}','reasoning.max_rounds':max_rounds}))
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            def api(path,body=None):
                req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=5) as response:return json.load(response)
            with (BUILD/f'contract-{protocol}-{mode}.log').open('w',encoding='utf8') as log:
                proc=subprocess.Popen([str(BINARY),'--state',folder,'--workspace',folder,'serve',str(port)],cwd=ROOT,env={**os.environ,'COA_TOOL_PROTOCOL':protocol},stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                try:
                    # The rollover fixture performs 288+ single-file writes; under
                    # AddressSanitizer on a shared runner that can exceed two
                    # minutes even though the task itself makes steady progress.
                    deadline=time.monotonic()+(240 if mode.startswith('rollover') or live_image_route else 25)
                    for _ in range(150):
                        try:api('/v1/scheduler');break
                        except OSError:assert proc.poll() is None;time.sleep(.05)
                    body={'message':prompt,'session':'contract'}
                    if mode=='ui_artwork':
                        route={'provider':'openai','model':'mock-text-to-image',
                               'base_url':f'http://127.0.0.1:{server.server_port}',
                               'api_key':'art-fixture'}
                        if live_image_route:
                            route={key:live_image_route[key] for key in route}
                        api('/v1/routes',{**route,'name':'art','caps':'image_generation'})
                    if protocol=='native' and mode in ('commentary','quoted_commentary','stuck'):
                        body['acceptance']={'requires_observation':True}
                    if acceptance is not None:body['acceptance']=acceptance
                    tid=api('/v1/chat',body)['id']
                    while True:
                        task=api(f'/v1/tasks/{tid}')
                        if task['status'] not in ('RUNNING','QUEUED'):break
                        assert time.monotonic()<deadline,task;time.sleep(.02)
                    assert task['status']==expected,(protocol,mode,task)
                    assert calls<=(20 if mode.startswith('rollover') else 16 if mode=='baseline_capacity' else 12 if mode in ('batch50','cycle_complete','cycle_missing') else 10),(mode,calls,task)
                    if expected=='DONE':
                        assert task['output']==FINAL,task
                        if mode in ('artifact','artifact_recovery'):assert (work/'report.any').read_text()==CONTENT
                        if mode=='script_output':assert (work/'report.any').read_text()==CONTENT
                        if mode=='ui_artwork':
                            image=(work/'assets'/'hero.png').read_bytes()
                            if live_image_route:
                                assert image.startswith(b'\x89PNG\r\n\x1a\n') and len(image)>len(base64.b64decode(png64))
                                print('Live UI hero image bytes:',len(image),flush=True)
                            else:
                                assert image==base64.b64decode(png64)
                                assert len(image_calls)==1 and image_calls[0]['model']=='mock-text-to-image',image_calls
                            assert (work/'index.html').read_text()==markup
                        if mode=='image_history':
                            # The terminal scheduler status can become visible
                            # just before the durable journal record lands
                            # (completion hook runs after the status change),
                            # so read history with a short bounded retry.
                            previews=[];history_deadline=time.monotonic()+5
                            while True:
                                runs=api('/v1/chat/history?session=contract')['runs']
                                previews=[s['image_preview'] for r in runs for event in (r.get('trace') or [])
                                          for s in event.get('steps',[]) if s.get('target')=='preview.png' and 'image_preview' in s]
                                if previews or time.monotonic()>=history_deadline:break
                                time.sleep(.02)
                            assert previews,runs
                            preview=previews[0]
                            assert set(preview)=={'id','name','session'} and preview['session']=='contract',preview
                            result=api('/v1/uploads/preview?id='+preview['id']+'&session=contract')
                            assert base64.b64decode(result['data_url'].split(',',1)[1])==png
                    else:assert not api('/v1/chat/history?session=contract')['turns']
                    if mode=='prefixed':assert task['tool_calls']==1 and task['model_failures']==0,task
                    if mode=='quoted_commentary':assert task['tool_calls']==1 and task['model_failures']==0,task
                    if mode=='commentary':assert task['tool_calls']==1 and task['model_failures']==0,task
                    if mode=='stuck':assert calls==3 and task['tool_calls']==0,task
                    if mode.startswith('declaration_'):
                        # Declaration acknowledgments must reach the next planner
                        # turn without running acceptance checks on absent receipts.
                        for request in declaration_requests[1:3]:
                            assert '验收合同已记录'.encode() in request,(protocol,mode,'no control receipt')
                            assert '下一轮必须调用实际需要的工具'.encode() in request,(protocol,mode,'no action guidance')
                            assert b'Previous completion was rejected:' not in request,(protocol,mode,'premature completion check')
                            assert b'no committed action receipt is present' not in request,(protocol,mode,'missing action checked too early')
                        if mode=='declaration_recovery':
                            assert task['tool_calls']==2 and task['model_failures']==0,task
                            assert (work/'report.any').read_text(encoding='utf8')==CONTENT
                        else:
                            assert calls==(1 if mode=='declaration_budget' else 3),task
                            assert task['tool_calls']==0 and task['model_failures']==0,task
                            assert '只声明验收合同' in task['output'],task
                            assert 'no committed action receipt is present' not in task['output'],task
                            assert not (work/'report.any').exists(),task
                    if mode=='full':assert task['tool_calls']==3,task
                    if mode.startswith('report_scope_'):
                        if expected=='DONE':
                            assert (work/'report.html').read_text(encoding='utf-8') == '<html>Verified report</html>'
                            assert not (work/'redirect.html').exists() and not (work/'wrong.md').exists()
                            assert (work/'appendix.md').exists() == (mode=='report_scope_multiple')
                            assert len(replies) <= calls <= len(replies)+1, (calls, task)
                        else:
                            assert not list(work.glob('*.html')), task
                            if mode=='report_scope_interleaved_stop':
                                assert calls==len(replies), (calls, task)
                                assert '累计尝试声明验收合同 4 次' in task['output'], task
                                assert '当前合同要求 1 个产物' in task['output'], task
                                assert 'report.html' in task['output'] and 'sandbox-analysis.html' not in task['output'], task
                            else:
                                assert 'no verified write in this run' in task['output'], task
                    if mode=='optional':assert task['tool_calls']==1,task
                    if mode.startswith('cache_'):
                        steps=task['steps']
                        assert len(steps)==3,steps
                        result=steps[-1]['out']
                        assert 'previous search result reused' not in result,steps
                        assert ('cache-target' if mode=='cache_shell_env' else 'review-proof') in result,steps
                    if mode in ('missing','erase'):assert task['tool_calls']==0 and not (work/'report.any').exists(),task
                    if mode=='script_reuse':
                        # Same process, same session/lane, same reasoning object:
                        # an earlier production receipt cannot satisfy a new run.
                        replies[:]=[declare(artifacts=['report.any']),action('shell',command='cat report.any',outputs=['report.any']),read,done]
                        calls=0
                        tid=api('/v1/chat',body)['id']
                        deadline=time.monotonic()+25
                        while True:
                            second=api(f'/v1/tasks/{tid}')
                            if second['status'] not in ('RUNNING','QUEUED'):break
                            assert time.monotonic()<deadline,second;time.sleep(.02)
                        assert second['status']=='FAILED',(protocol,'cross-run artifact accepted',second)
                        assert calls<=7,(calls,second)
                        print('PASS:',protocol,'same-process second run rejects old artifact',flush=True)
                    print('PASS #236:',protocol,mode,expected,'calls',calls,'tools',task['tool_calls'],flush=True)
                finally:proc.terminate();proc.wait(timeout=5)
    finally:server.shutdown();server.server_close()

if __name__=='__main__':
    for protocol in ('native', 'legacy'):
        for mode in ('report_scope_recovery', 'report_scope_fixed', 'report_scope_written', 'report_scope_multiple',
                     'report_scope_interleaved', 'report_scope_interleaved_stop', 'report_scope_undeclared',
                     'report_scope_no_output'):
            exercise(protocol, mode)
        exercise(protocol, 'chat', '分析「输出一个html分析报告」为什么失败。')
        exercise(protocol, 'chat', '不要输出一个 HTML 分析报告，只解释错误。')
    exercise('native','ui_artwork')
    exercise('native','image_history')
    exercise('native','script_late_contract')
    exercise('native','script_budget_readback')
    exercise('native','script_verified_edit')
    exercise('native','script_unverified_edit')
    exercise('native','cycle_complete')
    exercise('native','cycle_missing')
    if os.name=='nt':exercise('native','script_windows_case')
    for protocol in ('native','legacy'):exercise(protocol,'artifact_recovery')
    for protocol in ('native','legacy'):
        for mode in ('rollover','rollover_stale'):exercise(protocol,mode)
    for protocol in ('native','legacy'):exercise(protocol,'batch50')
    for protocol in ('native','legacy'):
        for mode in ('edit','edit_failed','edit_stale'):exercise(protocol,mode)
    exercise('native','plain_final')
    exercise('legacy','prefixed')
    for mode in ('cache_shell_branch','cache_git_tag'):
        exercise('native',mode)
    for protocol in ('native','legacy'):exercise(protocol,'baseline_capacity')
    for protocol in ('native','legacy'):
        for mode in ('unreceipted_rewrite','late_output_declaration','nonproducing_output_declaration'):exercise(protocol,mode)
    if os.name!='nt':exercise('native','cache_shell_env')
    for protocol in ('legacy','native'):
        for mode in ('declaration_recovery','declaration_loop','declaration_budget'):exercise(protocol,mode)
        for mode in ('chat','commentary','quoted_commentary','stuck','artifact','missing','erase','preexisting','write_only','stale','script_output','script_reuse','script_unchanged','script_rewrite','full','partial','small','optional','api_missing','observation','mutation','failed_tool'):
            exercise(protocol,mode)
        for prompt in ('为什么帮我解释如何修改代码并完整读取文件，输出HTML报告。','Do not read. Read all source. I will write. Help install fix create.','Прочитайте исходники. 完整读文件只是引用。'):
            exercise(protocol,'chat',prompt)
