/* 漫剧工房：预设角色（分镜导演/画师/配音/剪辑合成）可见，一键导入小说
 * 读取本地文件 → POST /v1/manju/import（title/episode/text）→ 状态提示、
 * 刷新 agent 池与协作任务列表。Mocked backend, no live server. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:process.env.UI_TEST_BROWSER||'msedge'});
 const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'manju-ui-'));
 const novel=path.join(tmp,'novel.txt');
 fs.writeFileSync(novel,'第一章 起点\n他推开门。\n"走吧。"她说。\n第二章 转折\n雨落下来。\n','utf8');
 try{
  const page=await browser.newPage({viewport:{width:1400,height:900}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  let importBody=null,agentsGets=0,flowLists=0;
  const agents=[{name:'coder',role:'开发'}];
  await page.route('http://ui.test/**',async route=>{
   const r=route.request(),u=new URL(r.url());let obj=[];
   if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'../apps/web/index.html'),'utf8')});
   if(u.pathname==='/v1/config/llm')obj={provider:'openai',model:'fixture',api_key_set:true};
   if(u.pathname==='/v1/routes')obj=[
    {name:'deepseek-script',provider:'openai',model:'deepseek-v4.1-flash-1017'},
    {name:'qwen-panels',provider:'openai',model:'qwen-image-3.0-pro',image_generation:true}
   ];
   if(u.pathname==='/v1/workspaces')obj={default_workspace:'D:/default',workspaces:[]};
   if(u.pathname==='/v1/agents'){
    if(r.method()==='POST'){const b=r.postDataJSON();agents.push({name:b.name,role:b.role});obj={ok:true,index:agents.length-1};}
    else{agentsGets++;obj={agents,facts:{}};}
   }
   if(u.pathname==='/v1/blackboard')obj={};
   if(u.pathname==='/v1/flows/list'){flowLists++;obj={records:importBody?[{id:7,name:'漫剧《测试小说》第1集',status:'RUNNING'}]:[]};}
   if(u.pathname==='/v1/manju/import'&&r.method()==='POST'){
    importBody=r.postDataJSON();
    obj={id:7,status:'queued',title:importBody.title,novel_dir:'novel/测试小说',chapters:2,episode:importBody.episode,pipeline:'tools/manju/manju_pipeline.py',agents:['manju-director','manju-artist','manju-voice','manju-editor']};
   }
   if(u.pathname==='/v1/chat/sessions'){
    if(r.method()==='POST')obj={id:'new1',workspace:'D:/default'};
    else obj={sessions:[],next_cursor:null};
   }
   if(u.pathname==='/v1/chat/history')obj={turns:[],runs:[]};
   return route.fulfill({contentType:'application/json',body:JSON.stringify(obj)});
  });
  await page.goto('http://ui.test/?desktop=1');
  await page.waitForFunction(()=>typeof switchPanel==='function',{timeout:15000});
  await page.evaluate(()=>switchPanel('agents'));
  /* 漫剧预设角色全部可见 */
  for(const label of ['分镜导演','画师','配音','剪辑合成']){
   await page.locator('#agent-presets .agent-preset',{hasText:label}).first().waitFor({timeout:5000});
  }
  await page.waitForFunction(()=>document.getElementById('manju-text-route').value==='deepseek-script' &&
    document.getElementById('manju-image-route').value==='qwen-panels');
  /* 一键导入：选文件 → 填标题 → 点击制作 */
  await page.locator('#manju-file').setInputFiles(novel);
  await page.locator('#manju-title').fill('测试小说');
  await page.locator('#manju-episode').fill('1');
  const before=agentsGets;
  await page.getByRole('button',{name:'🎬 导入并组队制作'}).click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('已切 2 章'),{timeout:10000});
  assert.ok(importBody,'import endpoint called');
  assert.equal(importBody.title,'测试小说');
  assert.equal(importBody.episode,1);
  assert.equal(importBody.text_route,'deepseek-script');
  assert.equal(importBody.image_route,'qwen-panels');
  assert.ok(importBody.text.includes('第一章 起点')&&importBody.text.includes('第二章 转折'),'novel text uploaded');
  assert.ok(!importBody.path,'file import must not send a server path');
  assert.ok(agentsGets>before,'agent pool refreshed after import');
  assert.ok(flowLists>0,'collaboration task list refreshed');
  const stat=await page.locator('#manju-stat').innerText();
  assert.ok(stat.includes('#7'),'queued task id surfaced');
  /* 空输入给出明确提示而不是发请求 */
  const callsBefore=importBody;
  await page.locator('#manju-title').fill('');
  await page.evaluate(()=>{document.getElementById('manju-file').value='';document.getElementById('manju-path').value='';});
  await page.getByRole('button',{name:'🎬 导入并组队制作'}).click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('先选择小说文件'),{timeout:5000});
  assert.equal(importBody,callsBefore,'no request without input');
  assert.deepEqual(errors,[]);
  console.log('PASS: manju presets, one-click novel import, team + flow refresh, empty-input guard');
 }finally{await browser.close();fs.rmSync(tmp,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;});
