/* Session sharing UI: import button + file flow, export menu download, and the
 * per-turn rewind dialog (chat-only vs chat+files, files disabled without
 * snapshots). Mocked backend, no live server. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.UI_TEST_BROWSER||'msedge'});
 try{
  const page=await browser.newPage({viewport:{width:1400,height:900}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const importBodies=[],rewindBodies=[];let exported=0;
  const firstPrompt='第一问\n原始 **Markdown** 与 `命令`';
  const sessions=[{id:'s1',title:'分享会话',turns:2,created_ms:1000,last_active_ms:2000,workspace:'D:/default'}];
  const bundle={version:1,kind:'coa-session',exported_ms:1,title:'分享会话',created_ms:1000,
    turns:[{q:'第一问',a:'答1'},{q:'第二问',a:'答2'}]};
  await page.route('http://ui.test/**',async route=>{
   const r=route.request(),u=new URL(r.url());let obj=[];
   if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'../apps/web/index.html'),'utf8')});
   if(u.pathname==='/v1/config/llm')obj={provider:'openai',model:'fixture',api_key_set:true};
   if(u.pathname==='/v1/routes')obj=[];
   if(u.pathname==='/v1/workspaces')obj={default_workspace:'D:/default',workspaces:[]};
   if(u.pathname==='/v1/agents')obj={agents:[{name:'coder',role:'开发'}],facts:{}};
   if(u.pathname==='/v1/blackboard')obj={};
   if(u.pathname==='/v1/flows/list')obj={records:[]};
   if(u.pathname==='/v1/chat/sessions'){
    if(r.method()==='POST')obj={id:'new1',workspace:'D:/default'};
    else obj={sessions,next_cursor:null};
   }
   if(u.pathname==='/v1/chat/sessions/import'&&r.method()==='POST'){
    importBodies.push(JSON.parse(r.postData()||'{}'));
    sessions.push({id:'imported1',title:'分享会话',turns:2,created_ms:1000,last_active_ms:3000,workspace:'D:/default'});
    obj={id:'imported1',workspace:'D:/default',turns:2};
   }
   if(u.pathname==='/v1/chat/sessions/s1/export'&&r.method()==='POST'){exported++;obj=bundle;}
   if(u.pathname==='/v1/chat/sessions/s1/rewind'&&r.method()==='POST'){
    rewindBodies.push(JSON.parse(r.postData()||'{}'));
    obj={ok:true,turns:0,prompt:'第一问',files_restored:2};
   }
   if(u.pathname==='/v1/chat/history'){
    const sid=u.searchParams.get('session');
    obj=sid==='s1'
      ?{turns:[{q:firstPrompt,a:'答1',snaps:['snap1']},{q:'第二问',a:'答2',snaps:['snap2']}],runs:[]}
      :{turns:[{q:'问题',a:'回答'}],runs:[]};
   }
   return route.fulfill({contentType:'application/json',body:JSON.stringify(obj)});
  });
  await page.goto('http://ui.test/?desktop=1');
  await page.evaluate(()=>switchPanel('chat'));
  /* Import button is advertised in the Recent sidebar. */
  const impBtn=page.locator('#side-import-session');
  await impBtn.waitFor({timeout:15000});
  /* Import flow: file chooser -> POST bundle -> switch to the new session. */
  const tmp=path.join(os.tmpdir(),'coa-share-ui-'+Date.now()+'.json');
  fs.writeFileSync(tmp,JSON.stringify(bundle),'utf8');
  const [chooser]=await Promise.all([page.waitForEvent('filechooser'),impBtn.click()]);
  await chooser.setFiles(tmp);
  await page.waitForFunction(()=>CHAT_SESSION==='imported1',{timeout:10000});
  assert.equal(importBodies.length,1);
  assert.equal(importBodies[0].kind,'coa-session');
  assert.equal(importBodies[0].turns.length,2);
  fs.unlinkSync(tmp);
  /* Export menu item downloads the portable bundle. */
  await page.evaluate(()=>chatSwitchSession('s1'));
  await page.waitForFunction(()=>CHAT_HISTORY_READY.has('s1'),{timeout:10000});
  await page.evaluate(()=>{const e={clientX:200,clientY:200};chatSessionMenu(e,'s1','分享会话');});
  const expItem=page.getByRole('menuitem',{name:'导出会话'});
  await expItem.waitFor({timeout:3000});
  const [download]=await Promise.all([page.waitForEvent('download'),expItem.click()]);
  assert.ok(download.suggestedFilename().startsWith('coa-session-'));
  assert.ok(download.suggestedFilename().endsWith('.json'));
  assert.equal(exported,1);
  /* #291: Copy and rewind are separate controls on each saved turn. */
  await page.evaluate(()=>{
   window.copiedPrompt=null;
   Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.copiedPrompt=text;}}});
  });
  const actions=page.locator('.chat-message.user > .chat-user-actions').first();
  const copy=actions.getByRole('button',{name:'复制这条消息'});
  const rewind=actions.getByRole('button',{name:'回滚到此'});
  await rewind.waitFor({timeout:5000});
  assert.equal(await copy.evaluate(el=>getComputedStyle(el).opacity),'1');
  await copy.click();
  assert.equal(await page.evaluate(()=>window.copiedPrompt),firstPrompt);
  assert.equal(rewindBodies.length,0,'copy never issues a rewind');
  assert.equal(await page.locator('#session-rewind').count(),0);
  await page.evaluate(()=>chatRedraw());
  assert.equal(await copy.count(),1,'redraw keeps one copy action per user turn');
  /* Rewind still offers both modes. */
  await rewind.click();
  const dlg=page.locator('#session-rewind');
  await dlg.waitFor({timeout:3000});
  assert.ok((await dlg.innerText()).includes('回滚将删除此后的 2 轮对话'));
  const bothBtn=dlg.locator('[data-act=both]');
  assert.ok(await bothBtn.isEnabled(),'snaps present -> file restore enabled');
  await dlg.locator('[data-act=chat]').click();
  await page.waitForFunction(()=>!document.getElementById('session-rewind'),{timeout:5000});
  assert.equal(rewindBodies.length,1);
  assert.deepEqual(rewindBodies[0],{turn:0,files:false});
  /* Without snapshots the file-restore option is disabled with a hint. */
  await page.evaluate(()=>chatRewindSession('s1',{turnIndex:0,snaps:[]}));
  const dlg2=page.locator('#session-rewind');
  await dlg2.waitFor({timeout:3000});
  assert.ok(await dlg2.locator('[data-act=both]').isDisabled());
  assert.ok((await dlg2.innerText()).includes('仅可回滚对话'));
  await dlg2.locator('[data-act=cancel]').click();
  assert.deepEqual(errors,[]);
  console.log('PASS: session import/export, copy beside rewind, rewind chat/files modes, files disabled without snapshots');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
