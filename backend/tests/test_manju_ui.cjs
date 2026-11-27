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
  let importBody=null,imports=0,agentsGets=0,flowLists=0,rejectImport=false,batchEpisodes=[];
  const agents=[{name:'coder',role:'开发'}];
  const castByWorkspace=new Map();
  await page.route('http://ui.test/**',async route=>{
   const r=route.request(),u=new URL(r.url());let obj=[];
   if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname,'../apps/web/index.html'),'utf8')});
   if(u.pathname==='/v1/config/llm')obj={provider:'openai',model:'fixture',api_key_set:true};
   if(u.pathname==='/v1/routes')obj=[
    {name:'deepseek-script',provider:'openai',model:'deepseek-v4.1-flash-1017'},
    {name:'qwen-panels',provider:'openai',model:'qwen-image-3.0-pro',image_generation:true},
    {name:'horse-video',provider:'openai',model:'happyhorse-1.1-t2v',video_generation:true},
    {name:'horse-first',provider:'openai',model:'happyhorse-1.1-i2v',video_generation:true,video_input:'first_frame'},
    {name:'horse-reference',provider:'openai',model:'happyhorse-1.1-r2v',video_generation:true,video_input:'reference'},
    {name:'seedance-video',provider:'openai',model:'doubao-seedance-2-0-260128',video_generation:true}
   ];
   if(u.pathname==='/v1/workspaces')obj={default_workspace:'D:/default',workspaces:[{path:'D:/film',name:'漫剧项目'}]};
   if(u.pathname==='/v1/manju/characters'){
    if(r.method()==='POST'){
     const {workspace,characters,voice_cast}=r.postDataJSON();
     obj={characters,voice_cast};
     castByWorkspace.set(workspace || 'D:/default',obj);
    }else{
     obj=castByWorkspace.get(u.searchParams.get('workspace') || 'D:/default') || {characters:[],voice_cast:{}};
    }
   }
   if(u.pathname==='/v1/agents'){
    if(r.method()==='POST'){const b=r.postDataJSON();agents.push({name:b.name,role:b.role});obj={ok:true,index:agents.length-1};}
    else{agentsGets++;obj={agents,facts:{}};}
   }
   if(u.pathname==='/v1/blackboard')obj={};
   if(u.pathname==='/v1/flows/list'){
    flowLists++;
    const batch=batchEpisodes.length ? {id:8,task_id:8,name:'漫剧批量制作',status:'RUNNING',
     dag:JSON.stringify({nodes:batchEpisodes.map(ep=>({manju_compose_episode:ep.episode}))})} : null;
    obj={records:batch?[batch]:importBody?[{id:7,name:'漫剧《测试小说》第1集',status:'RUNNING'}]:[]};
   }
   if(u.pathname==='/v1/tasks/8')obj={id:8,status:'RUNNING',flow_nodes:batchEpisodes.flatMap((ep,i)=>{
    const prefix=`ep${String(ep.episode).padStart(2,'0')}`;
    return [{id:`${prefix}-step1`,status:'ok',director_stage:'X-05',llm_calls:4},
      {id:`${prefix}-step3`,status:i===0?'ok':'running'}, {id:ep.compose_node,status:i===0?'ok':'queued'}];
   })};
   if(u.pathname==='/v1/manju/import'&&r.method()==='POST'){
    imports++;
    importBody=r.postDataJSON();
    if(rejectImport)return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'请重新核对角色对应关系'})});
    obj={id:7,status:'queued',title:importBody.title,novel_dir:'novel/测试小说',chapters:2,episode:importBody.episode,pipeline:'tools/manju/manju_pipeline.py',agents:['manju-director','manju-artist','manju-voice','manju-editor']};
    if(importBody.all_episodes){
     const last=importBody.episode_end ?? importBody.storyboards?.length ?? importBody.chapter_files?.length ?? 3;
     batchEpisodes=Array.from({length:last-importBody.episode+1},(_,i)=>{
      const episode=importBody.episode+i;
      return {episode,compose_node:`ep${String(episode).padStart(2,'0')}-step4`};
     });
     Object.assign(obj,{id:8,task_id:8,all_episodes:true,episodes:batchEpisodes});
    }
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
  await page.waitForFunction(()=>document.getElementById('manju-import-scope').value==='all');
  await page.getByLabel('制作范围').selectOption('single');
  await page.locator('#manju-title').fill('测试小说');
  await page.locator('#manju-episode').fill('1');
  const before=agentsGets;
  await page.getByRole('button',{name:'🎬 导入并组队制作'}).click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('已切 2 章'),{timeout:10000});
  assert.ok(importBody,'import endpoint called');
  assert.equal(importBody.title,'测试小说');
  assert.equal(importBody.episode,1);
  assert.equal(importBody.audio_mode,'dialogue','novel defaults to character dialogue');
  assert.equal(importBody.text_route,'deepseek-script');
  assert.equal(importBody.image_route,'qwen-panels');
  assert.equal(importBody.character_views,true);
  assert.ok(!('video_route' in importBody) && !('clip_seconds' in importBody));
  assert.ok(importBody.text.includes('第一章 起点')&&importBody.text.includes('第二章 转折'),'novel text uploaded');
  assert.ok(!importBody.path,'file import must not send a server path');
  await page.waitForFunction(()=>document.getElementById('flow-tasks').textContent.includes('测试小说'));
  assert.ok(agentsGets>=before,'agent pool loaded');
  assert.ok(flowLists>0,'collaboration task list refreshed');
  const stat=await page.locator('#manju-stat').innerText();
  assert.ok(stat.includes('#7'),'queued task id surfaced');
  const submit=page.getByRole('button',{name:'🎬 导入并组队制作'});
  await page.locator('#manju-media-mode').selectOption('video');
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('请选择视频模型'));
  assert.equal(imports,1,'video submission must select a route when several exist');
  await page.locator('#manju-video-route').selectOption('seedance-video');
  await page.locator('#manju-clip-seconds').selectOption('3');
  assert.ok(await page.locator('#manju-seedance-hint').isVisible());
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('已切 2 章'));
  assert.equal(imports,2);
  assert.equal(importBody.video_route,'seedance-video');
  assert.equal(importBody.clip_seconds,3);
  assert.equal(importBody.character_views,true);
  assert.equal(importBody.image_route,'qwen-panels');
  await page.locator('#manju-character-views').uncheck();
  assert.ok(await page.locator('#manju-image-fields').isHidden());
  await page.locator('#manju-video-route').selectOption('horse-video');
  await page.locator('#manju-clip-seconds').selectOption('4');
  assert.ok(await page.locator('#manju-seedance-hint').isHidden());
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('已切 2 章'));
  assert.equal(imports,3);
  assert.equal(importBody.video_route,'horse-video');
  assert.equal(importBody.clip_seconds,4);
  assert.ok(!('image_route' in importBody) && !('character_views' in importBody));
  await page.getByRole('button',{name:'加载三镜宣传样片'}).click();
  assert.equal(await page.locator('#manju-video-text').inputValue(),'horse-video');
  assert.equal(await page.locator('#manju-video-first-frame').inputValue(),'horse-first');
  assert.equal(await page.locator('#manju-video-reference').inputValue(),'horse-reference');
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('已导入 3 镜分镜'));
  assert.equal(imports,4);
  assert.ok(!('audio_mode' in importBody),'preview board keeps its authored playback');
  assert.deepEqual(importBody.video_routes,{text:'horse-video',first_frame:'horse-first',reference:'horse-reference'});
  assert.deepEqual(importBody.storyboard.shots.map(s=>s.video_mode),['text','first_frame','reference']);
  assert.ok(importBody.storyboard.shots[1].character_ids.includes(importBody.storyboard.shots[2].character_ids[0]));
  assert.equal(importBody.image_route,'qwen-panels');
  assert.equal(importBody.character_views,true);
  assert.ok(!('text_route' in importBody));
  assert.ok(await page.locator('#manju-progress').isVisible());
  /* 空输入给出明确提示而不是发请求 */
  const callsBefore=importBody;
  await page.locator('#manju-title').fill('');
  await page.evaluate(()=>{document.getElementById('manju-file').value='';document.getElementById('manju-path').value='';document.getElementById('manju-board-json').value='';});
  await page.getByRole('button',{name:'🎬 导入并组队制作'}).click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('先选择小说文件'),{timeout:5000});
  assert.equal(importBody,callsBefore,'no request without input');

  /* 人物图按角色分别添加，预览在本机，提交时才读入请求。 */
  await page.getByText('导入参考人物图',{exact:false}).click();
  const png='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO8ByKkAAAAASUVORK5CYII=';
  const picker=page.getByLabel('人物图片');
  const character=page.getByLabel('角色名');
  const addReference=page.getByRole('button',{name:'添加图片'});
  const pngFile=name=>({name,mimeType:'image/png',buffer:Buffer.from(png,'base64')});
  await page.locator('#manju-file').setInputFiles(novel);
  await page.waitForFunction(()=>document.getElementById('manju-import-scope').value==='all');
  await page.getByLabel('制作范围').selectOption('single');
  await page.locator('#manju-title').fill('参考角色测试');
  await page.locator('#manju-media-mode').selectOption('image');
  await picker.setInputFiles(pngFile('噜噜.png'));
  await addReference.click();
  await page.waitForFunction(()=>document.getElementById('manju-reference-status').textContent.includes('填写角色名'));
  await character.fill('噜噜');
  await addReference.click();
  await page.getByRole('img',{name:'噜噜 的参考图'}).waitFor();
  assert.match(await page.getByRole('img',{name:'噜噜 的参考图'}).getAttribute('src'),/^blob:/);
  await character.fill('噜妹');
  await picker.setInputFiles({name:'伪造.png',mimeType:'image/png',buffer:Buffer.from('not an image')});
  await addReference.click();
  await page.waitForFunction(()=>document.getElementById('manju-reference-status').textContent.includes('有效的 PNG'));
  await picker.setInputFiles([pngFile('噜妹-正面.png'),pngFile('噜妹-侧面.png')]);
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('请先点击「添加图片」'));
  assert.equal(imports,4,'unassigned photos must not be silently dropped');
  await addReference.click();
  const referenceList=page.getByRole('list',{name:'已添加的参考人物图'});
  await referenceList.getByRole('button',{name:'移除 噜妹 的参考图 噜妹-侧面.png'}).click();
  assert.equal(await referenceList.getByRole('img').count(),2);
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('画面方式请选择'));
  assert.equal(imports,4,'static generation must not silently ignore reference images');
  assert.equal(await page.locator('#manju-media-mode').inputValue(),'image');

  await page.locator('#manju-media-mode').selectOption('mixed');
  await page.locator('#manju-image-route').selectOption('');
  await page.locator('#manju-character-views').uncheck();
  rejectImport=true;
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('请重新核对角色对应关系'));
  assert.equal(await referenceList.getByRole('img').count(),2,'references survive the failed request');
  rejectImport=false;
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('已切 2 章'));
  assert.equal(imports,6);
  assert.deepEqual(importBody.character_references,[
   {name:'噜噜',filename:'噜噜.png',data_url:`data:image/png;base64,${png}`},
   {name:'噜妹',filename:'噜妹-正面.png',data_url:`data:image/png;base64,${png}`}
  ]);
  assert.deepEqual(importBody.video_routes,{text:'horse-video',first_frame:'horse-first',reference:'horse-reference'});
  assert.ok(!('image_route' in importBody) && !('character_views' in importBody));

  const novelFiles=page.getByLabel('小说或分镜剧本');
  const scope=page.getByLabel('制作范围');
  const txtFile=(name,text)=>({name,mimeType:'text/plain',buffer:Buffer.from(text)});
  await novelFiles.setInputFiles([txtFile('10-后记.md','雨落了。'),txtFile('2-开端.txt','噜噜打开门。')]);
  assert.equal(await scope.inputValue(),'all','separate files default to all chapters');
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('2 集已排队'));
  assert.equal(imports,7);
  assert.deepEqual(importBody.chapter_files,[{name:'2-开端.txt',text:'噜噜打开门。'},{name:'10-后记.md',text:'雨落了。'}]);
  assert.equal(importBody.episode,1);
  assert.equal(importBody.all_episodes,true);
  assert.ok(!('episode_end' in importBody) && !('text' in importBody));
  assert.deepEqual(importBody.character_references.map(ref=>ref.name),['噜噜','噜妹']);
  const progress=page.locator('[data-flow-task-id="8"] .flow-record-progress');
  await page.getByRole('button',{name:'查看制作进度'}).click();
  await page.waitForFunction(()=>document.querySelector('[data-flow-task-id="8"] .flow-record-progress')?.textContent.includes('多集进度 1/2 集已完成'));
  assert.match(await progress.innerText(),/第 2 集 · 制作中（配音）/);
  assert.match(await progress.innerText(),/铜铃 · 合成导演稿 · 模型请求 4/);

  await scope.selectOption('single');
  await novelFiles.setInputFiles(txtFile('全集.md','# 第1集 开头\n你好。\n# 第2集 下雨\n回家。\n# 第3集 见面\n开门。'));
  await page.waitForFunction(()=>document.getElementById('manju-import-scope').value==='all');
  await scope.selectOption('range');
  await page.getByLabel('起始章节').fill('2');
  await page.getByLabel('结束章节').fill('1');
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('结束章节需为'));
  assert.equal(imports,7,'invalid batch range must not submit');
  await page.getByLabel('结束章节').fill('3');
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('第 2–3 集'));
  assert.equal(imports,8);
  assert.ok(importBody.text.includes('# 第2集 下雨'));
  assert.equal(importBody.episode,2);
  assert.equal(importBody.episode_end,3);

  const storyboard=(title,text)=>({title,badge:'测试',shots:[{kind:'narration',text,speaker:'narrator',scene:'屋顶'}]});
  const first=storyboard('初见','你好。'), second=storyboard('归途','再见。');
  const jsonFile=(name,content)=>({name,mimeType:'application/json',buffer:Buffer.from(JSON.stringify(content))});
  await scope.selectOption('single');
  await novelFiles.setInputFiles(jsonFile('两集剧本.json',[first,second]));
  await page.waitForFunction(()=>document.getElementById('manju-import-scope').value==='all');
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('第 1–2 集'));
  assert.equal(imports,9);
  assert.deepEqual(importBody.storyboards,[first,second]);
  assert.ok(!('storyboard' in importBody) && !('text_route' in importBody));
  assert.ok(!('audio_mode' in importBody),'legacy storyboards keep their authored playback');

  await novelFiles.setInputFiles([jsonFile('02-归途.json',second),jsonFile('01-初见.json',first)]);
  await scope.selectOption('range');
  await page.getByLabel('起始章节').fill('1');
  await page.getByLabel('结束章节').fill('1');
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('1 集已排队'));
  assert.equal(imports,10);
  assert.deepEqual(importBody.storyboards,[first,second]);
  assert.equal(importBody.episode_end,1);

  const workspace=page.getByLabel('制作工作区');
  const cast=page.locator('#manju-cast');
  await workspace.selectOption('D:/film');
  assert.equal(await referenceList.getByRole('img').count(),0,'references stay in the workspace they were added to');
  await cast.locator('summary').first().click();
  await cast.getByLabel('角色 ID',{exact:true}).fill('yan');
  await cast.getByLabel('角色姓名').fill('阿砚');
  await cast.getByLabel('角色类型').selectOption('protagonist');
  await cast.getByLabel('角色音色').fill('zh-CN-YunxiNeural');
  await cast.getByLabel('语速调整').fill('-6');
  await cast.getByLabel('外观描述').fill('深色短发，肩上系着铜铃');
  await cast.getByText('角色版本（可选）',{exact:true}).click();
  await cast.getByLabel('外观 ID').fill('yan_young');
  const save=cast.getByRole('button',{name:'保存角色'});
  await save.click();
  await cast.getByRole('button',{name:'编辑角色 阿砚'}).waitFor();
  await cast.getByLabel('角色 ID',{exact:true}).fill('mother');
  await cast.getByLabel('角色姓名').fill('阿娘');
  await cast.getByLabel('角色音色').fill('zh-CN-XiaoxiaoNeural');
  await cast.getByLabel('音高调整').fill('-4');
  await cast.getByLabel('外观描述').fill('深色发髻，穿灰蓝布衣');
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('请先保存角色档案'));
  assert.equal(imports,10,'unfinished cast edits must not be silently dropped');
  await save.click();
  await cast.getByRole('button',{name:'编辑角色 阿娘'}).waitFor();
  await workspace.selectOption('');
  await page.waitForFunction(()=>!document.getElementById('manju-cast-list').children.length);
  assert.equal(await referenceList.getByRole('img').count(),2);
  await workspace.selectOption('D:/film');
  await cast.getByRole('button',{name:'编辑角色 阿砚'}).click();
  assert.equal(await cast.getByLabel('角色 ID',{exact:true}).inputValue(),'yan');
  assert.equal(await cast.getByLabel('外观 ID').inputValue(),'yan_young');
  assert.equal(await cast.getByLabel('语速调整').inputValue(),'-6');
  await cast.getByRole('button',{name:'清空表单'}).click();
  assert.ok(await cast.getByLabel('角色 ID',{exact:true}).isEditable());

  await scope.selectOption('single');
  await novelFiles.setInputFiles([txtFile('01.md','阿娘让阿砚把铜铃系好。'),txtFile('02.md','阿砚踏上山路。')]);
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('第 1–2 集'));
  assert.equal(imports,11);
  assert.equal(importBody.workspace,'D:/film');
  assert.equal(importBody.audio_mode,'dialogue');
  assert.deepEqual(importBody.character_roster,{
   characters:[
    {id:'yan',name:'阿砚',role:'protagonist',visual:'深色短发，肩上系着铜铃',appearance_id:'yan_young'},
    {id:'mother',name:'阿娘',role:'supporting',visual:'深色发髻，穿灰蓝布衣'}
   ],
   voice_cast:{
    yan:{voice:'zh-CN-YunxiNeural',rate:'-6%'},
    mother:{voice:'zh-CN-XiaoxiaoNeural',pitch:'-4Hz'}
   }
  });
  const audio=page.getByLabel('叙事配音');
  await audio.selectOption('legacy');
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('第 1–2 集'));
  assert.equal(imports,12);
  assert.equal(importBody.audio_mode,'legacy','legacy option is explicit');
  const authoredDialogue={
   title:'铜铃',badge:'回归',audio_mode:'dialogue',
   characters:importBody.character_roster.characters,
   shots:[{kind:'dialogue',speaker:'yan',text:'娘，我带上了。',character_ids:['yan','mother']}]
  };
  await scope.selectOption('single');
  await novelFiles.setInputFiles(jsonFile('对白.json',authoredDialogue));
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('分镜已设为角色对白'));
  assert.equal(imports,12,'dialogue boards must not be sent with contradictory legacy mode');
  await audio.selectOption('auto');
  await submit.click();
  await page.waitForFunction(()=>document.getElementById('manju-stat').textContent.includes('已导入 1 镜分镜'));
  assert.equal(imports,13);
  assert.deepEqual(importBody.storyboard,authoredDialogue);
  assert.deepEqual(Object.keys(importBody.character_roster.voice_cast),['yan','mother']);
  assert.ok(!('audio_mode' in importBody));

  assert.deepEqual(errors,[]);
  console.log('PASS: manju imports, reference photos, workspace cast, dialogue mode, and batch episode progress');
 }finally{await browser.close();fs.rmSync(tmp,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;});
