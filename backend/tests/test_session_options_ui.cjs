const {chromium}=require('playwright'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true,channel:process.env.UI_TEST_BROWSER||'msedge'});try{
 const page=await browser.newPage(),sessions=[],sessionRequests=[],submitted=[],errors=[];page.on('pageerror',e=>errors.push(e.message));let globalWrites=0;
 let petTask={id:0,input:'检视代码并发现优化点',status:'RUNNING',stage:'executing',cur_tool:'file_edit',steps:[
  {tool:'file_edit',ok:1,target:'example.c'},{tool:'file_edit',ok:1,target:'example.c'},
  {tool:'file_edit',ok:0,target:'unmodified.c'}]};
 await page.addInitScript(()=>{
  window.petMessages=[];
  window.chrome ??= {};
  const bridge=new EventTarget();
  window.petState='closed';
  window.setPetState=state=>{
   window.petState=state;
   bridge.dispatchEvent(new MessageEvent('message',{data:'task-pet:'+state}));
  };
  bridge.postMessage=message=>{
   window.petMessages.push(message);
   if(message==='task-pet:query')window.setPetState(window.petState);
   if(message==='task-pet:open')window.setPetState('open');
   if(message==='task-pet:close')window.setPetState('closed');
  };
  window.chrome.webview=bridge;
 });
 await page.route('http://ui.test/**',async route=>{
 const r=route.request(),u=new URL(r.url());let obj=[];
 if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'../apps/web/index.html'),'utf8')});
 if(u.pathname==='/v1/config/llm'){if(r.method()==='POST')globalWrites++;obj={provider:'openai',model:'global',api_key_set:true};}
 if(u.pathname==='/v1/routes')obj=[{name:'route-a',provider:'openai',model:'a'},{name:'route-b',provider:'openai',model:'b'}];
 if(u.pathname==='/v1/workspaces')obj={default_workspace:'D:/default',workspaces:[]};
 if(u.pathname==='/v1/chat/sessions'){if(r.method()==='POST'){sessionRequests.push(r.postDataJSON());obj={id:sessions.length?'b':'a',workspace:'D:/default'};sessions.push(obj);}else obj=sessions;}
 if(u.pathname==='/v1/chat/history')obj={turns:[],runs:[]};
 if(u.pathname==='/v1/scheduler')obj=petTask.status==='DONE'?
  {active:0,queued:0,current_task_id:-1}:{active:2,queued:1,current_task_id:0};
 if(u.pathname==='/v1/tasks/0')obj=petTask;
 if(u.pathname==='/v1/chat'){submitted.push(r.postDataJSON());obj={id:7};}
 if(u.pathname==='/v1/tasks/7')obj={id:7,status:'DONE',output:'verified'};
 return route.fulfill({contentType:'application/json',body:JSON.stringify(obj)});});
 await page.goto('http://ui.test/?desktop=1');
 await page.waitForFunction(()=>CHAT_SESSION==='a' && $('chat-model').options.length===3).catch(error=>{
  throw new Error(errors.join('\n') || error.message);
 });
 const pet=page.locator('#chat-task-pet');
 assert.equal(await pet.innerText(),'开启桌宠');
 await pet.click();
 await page.waitForFunction(()=>petMessages.includes('task-pet:status:move\n检视代码并发现优化点\n正在修改文件 · 已修改 1 个文件'));
 petTask={...petTask,stage:'planning',steps:[]};
 await page.waitForFunction(()=>petMessages.includes('task-pet:status:think\n检视代码并发现优化点\n正在规划下一步'));
 petTask={...petTask,status:'DONE'};
 await page.waitForFunction(()=>petMessages.includes('task-pet:status:celebrate\n检视代码并发现优化点\n已完成'));
 assert.equal(await pet.innerText(),'关闭桌宠');
 await pet.click();
 assert.equal(await pet.innerText(),'开启桌宠');
 assert.ok(await page.evaluate(()=>petMessages.includes('task-pet:close') && taskPetTimer===null));
 await pet.click();
 await page.evaluate(()=>setPetState('closed')); // The window was closed outside the web console.
 assert.equal(await pet.innerText(),'开启桌宠');
 assert.ok(await page.evaluate(()=>taskPetTimer===null));
 await page.locator('#chat-model').selectOption('route-a');await page.locator('#chat-permission').selectOption('ask');await page.locator('#chat-effort-menu summary').click();

 await page.locator('#chat-effort').fill('3');
 await page.locator('#chat-effort-menu summary').click();
 await pet.click();
 await page.locator('#chat-composer-toggle').click();
 await page.evaluate(()=>{
  switchPanel('agents');
  CHAT_WORKSPACE='D:/selected';
  window.chrome.webview.dispatchEvent(new MessageEvent('message',{data:'task-pet:new-chat'}));
 });
 await page.waitForFunction(()=>current==='chat' && CHAT_SESSION==='b' && $('chat-model').value==='@cur' && document.activeElement===$('chat-input'));
 assert.equal(sessionRequests.length,2,'desktop pet creates exactly one new session');
 assert.deepEqual(sessionRequests[1],{workspace:'D:/selected'});
 assert.equal(await page.locator('#chat-composer-toggle').getAttribute('aria-expanded'),'true');
 assert.equal(await page.locator('#chat-permission').inputValue(),'auto');assert.equal(await page.locator('#chat-effort-summary').innerText(),'默认');
 await page.locator('#chat-model').selectOption('route-b');await page.locator('#chat-permission').selectOption('full');
 await page.evaluate(()=>chatSwitchSession('a'));await page.waitForFunction(()=>$('chat-model').value==='route-a');
 assert.equal(await page.locator('#chat-permission').inputValue(),'ask');assert.equal(await page.locator('#chat-effort-summary').innerText(),'高度');
 await page.locator('#chat-input').fill('message for A');await page.evaluate(()=>{const upload=new Promise(r=>window.finishUpload=r);CHAT_ATTACH.push({id:'fixture-image',name:'fixture.png',kind:'image',status:'ready',upload});window.submission=chatSubmit();});
 await page.waitForFunction(()=>CHAT_SUBMITTING.has('a'));await page.evaluate(()=>chatSwitchSession('b'));await page.locator('#chat-input').fill('draft for B');
 await page.evaluate(()=>finishUpload());await page.waitForFunction(()=>!CHAT_SUBMITTING.has('a'));await page.evaluate(()=>submission);
 assert.equal(submitted[0].session,'a');assert.equal(submitted[0].route,'route-a');assert.equal(submitted[0].permission_mode,'ask');assert.equal(submitted[0].reasoning_effort,'high');assert.ok(!('thinking' in submitted[0]));
 assert.equal(await page.locator('#chat-input').inputValue(),'draft for B','waiting submission must not clear another chat draft');
 assert.equal(await page.evaluate(()=>CHAT_MSGS.some(m=>m.text==='message for A')),false,'pending attachment submission belongs only to original conversation');
 await page.evaluate(()=>switchPanel('agents'));await page.locator('#agent-model option[value="openai|a"]').waitFor({state:'attached'});await page.locator('#agent-model').selectOption('openai|a');await page.evaluate(()=>fillAgentModelSelect());assert.equal(await page.locator('#agent-model').inputValue(),'openai|a');assert.deepEqual(errors,[]);
 assert.equal(globalWrites,0);console.log('PASS #222: per-chat model/access/effort, fresh defaults, switching restore and immutable settings while attachment upload waits');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
