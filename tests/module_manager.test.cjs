const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const managerSource=fs.readFileSync(path.join(__dirname,'../modules/module_manager/__init__.py'),'utf8').split('<script>')[1].split('</script>')[0];
const settingsSource=fs.readFileSync(path.join(__dirname,'../modules/settings/__init__.py'),'utf8');
const checkedAt='2026-09-28T10:30:00+02:00';
function element(){
  let text='',html='';
  return {hidden:false,disabled:false,style:{},dataset:{},children:[],
    classList:{toggle(){},add(){}},addEventListener(){},setAttribute(){},removeAttribute(){},
    appendChild(child){this.children.push(child)},
    set textContent(value){text=String(value);html=text.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')},
    get textContent(){return text},
    set innerHTML(value){html=value;this.children=[]},get innerHTML(){return html}
  };
}
async function harness(){
  const elements=new Map(),requests=[];
  const el=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id)};
  const ctx=vm.createContext({document:{getElementById:el,querySelectorAll:()=>[],createElement:element,addEventListener(){}},
    fetch:async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({modules:[]})}},
    setTimeout(){},confirm:()=>true,alert(){},AbortSignal});
  vm.runInContext(managerSource.replace('})();','globalThis.manager={state,render,refresh,poll,loadCatalog,install};})();'),ctx);
  await new Promise(resolve=>setImmediate(resolve));
  return {el,ctx,requests,manager:ctx.manager};
}
function moduleItem(id,extra={}){
  return {id,name:id,summary:'Module description',version:'2.0',installed_version:'1.0',installed:true,update_available:true,
    publisher_type:'official',channel:'stable',compatible:true,missing_dependencies:[],...extra};
}
test('opening Module Manager only reads local state and does not claim everything is current',async()=>{
  const h=await harness();
  assert.deepEqual(h.requests.map(x=>x.url).sort(),['/api/module-manager/catalog','/api/module-manager/local','/api/module-manager/status']);
  assert.equal(h.el('mmTime').textContent,'Not checked yet');
  assert.match(h.el('mmUpdateSummary').textContent,/Check for module updates/);
  assert.equal(h.el('mmUpdateCount').hidden,true);
});
test('all update channels stay together above every filter, without new installs or duplicates',async()=>{
  const h=await harness();
  h.manager.state.checkedAt=checkedAt;
  h.manager.state.catalog=[moduleItem('gallery'),moduleItem('beta',{channel:'beta'}),moduleItem('community',{publisher_type:'community'}),
    moduleItem('new',{installed:false,update_available:false}),moduleItem('current',{update_available:false}),
    moduleItem('settings',{protected:true})];
  h.manager.state.local=h.manager.state.catalog.filter(x=>x.installed);
  for(const tab of ['installed','official','beta','community']){
    h.manager.state.tab=tab;h.manager.render();
    assert.equal(h.el('mmUpdateCount').textContent,'3');
    const updates=h.el('mmUpdatesGrid').innerHTML;
    for(const id of ['gallery','beta','community']){
      assert.ok(updates.includes(`data-install="${id}"`));
      assert.ok(!h.el('mmGrid').innerHTML.includes(`data-install="${id}"`));
      assert.ok(!h.el('mmGrid').innerHTML.includes(`data-remove="${id}"`));
    }
    assert.ok(!updates.includes('data-install="new"'));
    assert.ok(!updates.includes('data-install="settings"'));
    assert.match(updates,/Installed v1.0 &rarr; <strong>Available v2.0/);
    assert.match(updates,/mm-btn update/);
  }
  h.manager.state.tab='official';h.manager.render();
  assert.match(h.el('mmGrid').innerHTML,/data-install="new">Install module/);
});
test('incompatible and dependency-blocked updates remain counted but cannot be installed',async()=>{
  const h=await harness();h.manager.state.checkedAt=checkedAt;
  h.manager.state.catalog=[moduleItem('future',{compatible:false,minimum_hub_version:'9.0'}),moduleItem('dependent',{missing_dependencies:['viewer']})];
  h.manager.render();
  assert.equal(h.el('mmUpdateCount').textContent,'2');
  const html=h.el('mmUpdatesGrid').innerHTML;
  assert.match(html,/disabled>Requires CyberHub 9.0/);
  assert.match(html,/disabled>Needs viewer/);
  assert.ok(!html.includes('data-install='));
});
test('a failed first check stays unchecked; failure after success keeps dated results',async()=>{
  const h=await harness();
  h.ctx.fetch=async()=>({ok:false,json:async()=>({error:'Offline'})});
  await h.manager.refresh();
  assert.equal(h.el('mmNote').hidden,false);
  assert.match(h.el('mmNote').textContent,/failed: Offline/);
  assert.equal(h.el('mmTime').textContent,'Not checked yet');
  h.manager.state.checkedAt=checkedAt;h.manager.state.catalog=[moduleItem('gallery')];
  await h.manager.refresh();
  assert.equal(h.el('mmUpdateCount').textContent,'1');
  assert.match(h.el('mmNote').textContent,/last successful check/);
  assert.equal(h.el('mmRefresh').disabled,false);
});
test('successful check with no updates has a distinct result and clears an old error',async()=>{
  const h=await harness();h.el('mmNote').hidden=false;
  h.ctx.fetch=async url=>{assert.equal(url,'/api/module-manager/catalog?refresh=1');return {ok:true,json:async()=>({checked_at:checkedAt,modules:[]})}};
  await h.manager.refresh();
  assert.equal(h.el('mmNote').hidden,true);
  assert.match(h.el('mmUpdateSummary').textContent,/No updates found/);
  assert.equal(h.el('mmUpdateCount').hidden,true);
});
test('check button remains disabled while the check is in flight',async()=>{
  const h=await harness();let complete;
  h.ctx.fetch=()=>new Promise(resolve=>{complete=resolve});
  const check=h.manager.refresh();
  assert.equal(h.el('mmRefresh').disabled,true);
  h.manager.render();assert.equal(h.el('mmRefresh').disabled,true);
  complete({ok:true,json:async()=>({checked_at:checkedAt,modules:[]})});await check;
  assert.equal(h.el('mmRefresh').disabled,false);
});
test('after installation results are refreshed locally and the completed update disappears',async()=>{
  const h=await harness();h.requests.length=0;
  h.manager.state.checkedAt=checkedAt;h.manager.state.catalog=[moduleItem('gallery')];
  h.ctx.fetch=async url=>{
    h.requests.push({url});
    const data=url.endsWith('/status')?{stage:'done',restart_required:true}:
      url.endsWith('/local')?{modules:[moduleItem('gallery',{version:'2.0'})]}:
      {checked_at:checkedAt,modules:[moduleItem('gallery',{installed_version:'2.0',update_available:false})]};
    return {ok:true,json:async()=>data};
  };
  await h.manager.poll();
  assert.equal(h.el('mmRestart').hidden,false);
  assert.equal(h.el('mmUpdateCount').hidden,true);
  assert.equal(h.el('mmUpdatesGrid').innerHTML,'');
  assert.ok(h.requests.every(x=>!x.url.includes('refresh=1')));
});
test('community update still needs confirmation and sends only the chosen module',async()=>{
  const h=await harness();h.manager.state.catalog=[moduleItem('community',{publisher_type:'community'})];
  let question;h.ctx.confirm=text=>{question=text;return false};
  const before=h.requests.length;await h.manager.install('community');
  assert.match(question,/Update community 2.0/);assert.match(question,/executable code/);
  assert.equal(h.requests.length,before);
  h.ctx.confirm=()=>true;await h.manager.install('community');
  const request=h.requests.find(x=>x.url.endsWith('/install'));
  assert.equal(request.options.method,'POST');
  assert.deepEqual(JSON.parse(request.options.body),{module:'community'});
});
test('system updates omit individual module packages and do not claim modules are current',()=>{
  const elements=new Map();
  const el=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id)};
  const source=settingsSource.split('var _githubUpdateInfo = null;')[1].split('async function checkCyberHubUpdates()')[0];
  const ctx=vm.createContext({document:{getElementById:el,createElement:element}});
  vm.runInContext('var _githubUpdateInfo = null;'+source,ctx);
  ctx.data={packages:[{type:'hub',name:'CyberHub',version:'1.5',update_available:false},{type:'module',name:'Gallery',update_available:true}]};
  vm.runInContext('renderCyberHubUpdate(data)',ctx);
  assert.equal(el('githubUpdatePackages').children.length,1);
  assert.match(el('githubUpdatePackages').children[0].textContent,/Check Module Manager/);
  ctx.data.packages[0].update_available=true;
  vm.runInContext('renderCyberHubUpdate(data)',ctx);
  assert.equal(el('githubUpdatePackages').children.length,1);
  assert.equal(el('githubUpdatePackages').children[0].children[1].textContent,'Install system update');
});
