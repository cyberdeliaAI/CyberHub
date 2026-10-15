const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const server = fs.readFileSync(path.join(__dirname, '../core/server.py'), 'utf8');
const settings = fs.readFileSync(path.join(__dirname, '../modules/settings/__init__.py'), 'utf8');
const badgeScript = server.match(/_UPDATE_NOTIFICATION_JS = r"""([\s\S]*?)"""/)[1];
const settingsScript = 'window.hubRenderUpdateNotifications = function(data)' +
  settings.split('window.hubRenderUpdateNotifications = function(data)')[1].split('\nfunction loadAll()')[0];

function harness() {
  const elements = new Map();
  const make = () => ({hidden: false, textContent: '', title: '', disabled: false, checked: false, children: [],
    replaceChildren(...children) { this.children = children; }, append(...children) { this.children.push(...children); },
    appendChild(child) { this.children.push(child); }});
  const el = id => {if (!elements.has(id)) elements.set(id, make()); return elements.get(id);};
  const requests = [], timers = [];
  const document = {readyState:'loading', addEventListener(){}, getElementById:el, createElement:make,
    querySelectorAll(selector) {return [el(selector)];}};
  const ctx = vm.createContext({window:{}, document, Date, setTimeout(fn,ms){timers.push({fn,ms});return timers.length;},
    clearTimeout(){}, fetch:async (url,options) => {requests.push({url,options});return {ok:true,json:async()=>({count:0,checking:false})};}});
  vm.runInContext(badgeScript + settingsScript, ctx);
  return {ctx,el,requests,timers};
}
test('badge polling only reads local status and never triggers a remote check', async () => {
  const h=harness();
  await h.ctx.window.hubRefreshUpdateNotifications();
  assert.equal(h.requests.length,1);
  assert.equal(h.requests[0].url,'/api/updates/status');
  assert.ok(!h.requests[0].options.method);
  assert.equal(h.el('[data-hub-update-count]').hidden,true);
  assert.equal(h.timers.at(-1).ms,60000);
});
test('cached update badge and links render as text, including incompatible versions', async () => {
  const h=harness();
  h.ctx.fetch=async()=>({ok:true,json:async()=>({count:2,automatic_check:false,checking:true,
    checked_at:{system:'2026-10-12T12:00:00+02:00'},errors:{},updates:[
      {type:'hub',name:'CyberHub',current_version:'1.4.2',version:'1.5',compatible:true},
      {type:'module',name:'<img onerror=bad()>',current_version:'1',version:'2',compatible:false,minimum_hub_version:'9'}]})});
  await h.ctx.window.hubRefreshUpdateNotifications();
  assert.equal(h.el('[data-hub-update-count]').textContent,'2');
  assert.equal(h.el('[data-hub-update-count]').hidden,false);
  assert.equal(h.el('[data-hub-update-summary]').textContent,'2 updates available');
  assert.equal(h.el('updateNotificationsCheck').disabled,true);
  assert.equal(h.timers.at(-1).ms,1500);
  const rows=h.el('updateNotificationsList').children;
  assert.equal(rows[0].children[1].href,'/settings#software-updates');
  assert.equal(rows[1].children[1].href,'/module_manager#module-updates');
  assert.match(rows[1].children[0].textContent,/<img onerror=bad\(\)>.*requires CyberHub 9/);
});
test('failed offline check is distinguished from no updates and preserves cached notices', () => {
  const h=harness();
  h.ctx.window.hubRenderUpdateNotifications({count:1,checking:false,automatic_check:false,
    checked_at:{modules:'2026-10-12T12:00:00+02:00'},errors:{modules:'Offline'},updates:[]});
  assert.match(h.el('updateNotificationsStatus').textContent,/Offline.*last known results/);
  assert.match(h.el('updateNotificationsStatus').textContent,/1 update available/);
});
test('manual combined check is an explicit POST and cannot install anything', async () => {
  const h=harness();
  await h.ctx.checkAllUpdates();
  assert.equal(h.requests[0].url,'/api/updates/check');
  assert.equal(h.requests[0].options.method,'POST');
  assert.equal(h.requests[1].url,'/api/updates/status');
  assert.ok(h.requests.every(r=>!r.url.includes('/install')));
});
test('preference is opt-in, saved as a boolean, and remote controls are disabled', async () => {
  const h=harness();
  await h.ctx.saveUpdatePreference(true);
  assert.deepEqual(JSON.parse(h.requests[0].options.body),{path:'updates.automatic_check',value:true});
  h.ctx.window.hubRenderUpdateNotifications({count:0,checking:false,automatic_check:true,can_check:false,updates:[]});
  assert.equal(h.el('automaticUpdateChecks').disabled,true);
  assert.equal(h.el('updateNotificationsCheck').disabled,true);
});
