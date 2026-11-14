/* Regression: session titles must render (a stray undefined variable used to
 * abort tab rendering), history preload must stay bounded so panel switches
 * are not starved, and the thinking block must scroll with a visible meta line
 * (elapsed · ↑ tokens · thought for Xs). Mocked backend, no live server. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.UI_TEST_BROWSER||'msedge'});
 try{
  const page=await browser.newPage({viewport:{width:1400,height:900}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const historyCalls=[];let inflight=0,maxInflight=0,created=0;
  const allSessions=Array.from({length:30},(_,i)=>({id:'s'+(i+1),title:'话题 '+(i+1),turns:1,created_ms:1000+i,last_active_ms:1000+i,workspace:'D:/default'}));
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
    if(r.method()==='POST'){created++;obj={id:'new'+created,workspace:'D:/default'};}
    else{
     const limit=Number(u.searchParams.get('limit')||12),start=Number(u.searchParams.get('cursor')||0);
     obj={sessions:allSessions.slice(start,start+limit),next_cursor:start+limit<allSessions.length?String(start+limit):null};
    }
   }
   if(u.pathname==='/v1/chat/history'){
    inflight++;maxInflight=Math.max(maxInflight,inflight);
    historyCalls.push(u.searchParams.get('session'));
    await new Promise(res=>setTimeout(res,40));
    inflight--;
    obj={turns:[{q:'问题',a:'回答'}],runs:[]};
   }
   return route.fulfill({contentType:'application/json',body:JSON.stringify(obj)});
  });
  await page.goto('http://ui.test/?desktop=1');
  await page.waitForSelector('#side-recent .recent-session-title',{timeout:15000});
  await page.waitForFunction(()=>[...document.querySelectorAll('#side-recent .recent-session-title')].some(n=>n.textContent.includes('话题')),{timeout:10000});
  /* Entering agents must work while the history preload is still running. */
  await page.evaluate(()=>switchPanel('agents'));
  await page.waitForSelector('#agents-out2 table',{timeout:10000});
  await page.evaluate(()=>switchPanel('chat'));
  await page.waitForFunction(()=>CHAT_HISTORY_READY.size>0,{timeout:15000});
  await page.waitForTimeout(1500);
  assert.ok(historyCalls.length<=13,'preload bounded to the newest page, got '+historyCalls.length);
  assert.ok(maxInflight<=2,'preload concurrency must stay <=2, got '+maxInflight);
  assert.ok(!historyCalls.includes('s30'),'older cursor pages are not preloaded');
  const recent=await page.locator('#side-recent .recent-session-title').allInnerTexts();
  assert.ok(recent.some(t=>t.includes('话题')),'recent list shows real titles');
  assert.ok(!recent.every(t=>t==='新会话'),'recent titles must not all fall back to 新会话');
  /* Recent row context menu works. */
  await page.locator('#side-recent .side-recent-row > button').first().click({button:'right'});
  await page.getByRole('menuitem',{name:'修改会话标题 / 简介'}).waitFor({timeout:3000});
  await page.keyboard.press('Escape');
  /* Thinking block: scrollable body with a meta line that stays visible. */
  await page.evaluate(()=>{
    chatAddBubble('bot','');
    const entry=CHAT_MSGS[CHAT_MSGS.length-1];
    entry.pending=false;entry.taskId=99100;
    entry.progress={stage:'executing',seq:5,elapsed_ms:463000,
      tokens_in:10900,tokens_out:800,usage_available:true,llm_ms:18000,round:3,tool_calls:4,
      steps:[{id:1,tool:'file_read',target:'a.c',ok:1,ms:5}]};
    entry.progressEvents=[];
    renderChatEntry(entry);
    window.testThoughtEntry=entry;
  });
  await page.locator('#chat-execution-toggle').click();
  await page.locator('.run-collapse').last().click();
  const meta=page.locator('.run-thought-meta').last();
  await meta.waitFor({state:'visible',timeout:5000});
  assert.equal(await meta.innerText(),'7m 43s · ↑ 10.9k tokens · thought for 18s');
  const body=page.locator('.run-thought-body').last();
  const css=await body.evaluate(el=>({overflow:getComputedStyle(el).overflowY,max:getComputedStyle(el).maxHeight}));
  assert.equal(css.overflow,'auto','thinking body must scroll');
  assert.equal(css.max,'280px');
  await body.evaluate(el=>{el.style.maxHeight='60px';el.style.scrollBehavior='auto';el.scrollTop=40;});
  assert.ok(await meta.isVisible(),'meta line stays visible while scrolling');
  const boxes=await page.evaluate(()=>{
   const m=document.querySelector('.run-thought-meta').getBoundingClientRect();
   const b=document.querySelector('.run-thought-body').getBoundingClientRect();
   return {metaBottom:m.bottom,bodyTop:b.top,scroll:document.querySelector('.run-thought-body').scrollTop};
  });
  assert.ok(boxes.scroll>0,'body actually scrolled');
  assert.ok(boxes.metaBottom<=boxes.bodyTop+1,'meta sits above the scroll area');
  await page.evaluate(()=>{const e=window.testThoughtEntry;e.pending=true;e.progress.started_ms=Date.now()-463000;e.progress.llm_ms=25000;chatProgressClock(e);});
  assert.ok((await meta.innerText()).includes('thought for 25s'),'live clock refreshes the meta line');
  assert.deepEqual(errors,[]);
  console.log('PASS: real session titles, bounded preload, agents entry during preload, tab menu, scrollable thinking with sticky meta');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
