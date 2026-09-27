const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../modules/settings/__init__.py'),'utf8').split('let aiConnectionConfig = {}')[1].split('var _restart')[0];
function harness(){
 const elements=new Map();
 const el=id=>{if(!elements.has(id))elements.set(id,{value:'',checked:false,replaceChildren(){},reportValidity:()=>true});return elements.get(id);};
 const ctx=vm.createContext({document:{getElementById:el,querySelectorAll:()=>[],createElement:()=>({})},AbortSignal});
 vm.runInContext('let aiConnectionConfig = {};'+source,ctx);
 return {el,ctx,run:s=>vm.runInContext(s,ctx)};
}
test('central key is masked, preserved by omission, and removed explicitly',()=>{
 const h=harness();h.run('populateAiConnection({api_url:"http://model/v1",api_key_set:true})');
 assert.equal(h.el('aiApiKey').value,'');assert.match(h.el('aiApiKey').placeholder,/saved/);
 assert.equal(Object.hasOwn(h.run('aiConnectionDraft()'),'api_key'),false);
 h.el('aiApiKey').value='new-secret';assert.equal(h.run('aiConnectionDraft().api_key'),'new-secret');
 h.el('aiClearApiKey').checked=true;assert.equal(h.run('aiConnectionDraft().api_key'),'');
});
test('central browser test uses a saved bearer key without changing settings',async()=>{
 const h=harness();h.run('populateAiConnection({api_url:"http://model/v1",api_key_set:true,transport:"browser"})');
 const calls=[];h.ctx.fetch=async(url,opts)=>{calls.push({url,opts});return {ok:true,json:async()=>url.endsWith('/browser-connection')?{api_url:'http://model/v1',api_key:'saved-secret'}:{data:[{id:'vision'}]}};};
 await h.run('testAiConnection()');
 assert.equal(calls.length,2);assert.equal(calls[1].url,'http://model/v1/models');
 assert.equal(calls[1].opts.headers.Authorization,'Bearer saved-secret');assert.equal(calls[1].opts.redirect,'error');
 assert.equal(h.run('aiConnectionConfig.api_key'),undefined);assert.equal(h.el('aiApiKey').value,'');
});
test('clearing the central key sends no Authorization header on browser test',async()=>{
 const h=harness();h.run('populateAiConnection({api_url:"http://model/v1",api_key_set:true,transport:"browser"})');h.el('aiClearApiKey').checked=true;
 h.ctx.fetch=async(url,opts)=>{assert.equal(url,'http://model/v1/models');assert.equal(opts.headers.Authorization,undefined);return {ok:true,json:async()=>({data:[]})};};
 await h.run('testAiConnection()');
});
