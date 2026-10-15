const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../modules/settings/__init__.py'), 'utf8');
const script = 'function diagnosticGPUs(d)' + source.split('function diagnosticGPUs(d)')[1]
  .split('/* No auto-load')[0];

function harness(data) {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, {innerHTML: '', classList: {add(){}, remove(){}}});
    return elements.get(id);
  };
  let clipboard = '';
  const ctx = vm.createContext({_lastSysInfo: data, document: {getElementById: el},
    navigator: {clipboard: {writeText(text){clipboard = text; return Promise.resolve();}}},
    setTimeout(){}});
  vm.runInContext(script, ctx);
  return {ctx, el, clipboard: () => clipboard};
}
function report(overrides = {}) {
  return {python: '3.14.8', python_executable: '/hub/.venv/bin/python', gpus: ['AMD RX 580', 'NVIDIA RTX 3060'],
    onnx_runtime: {status: 'available', version: '1.31.0', providers: ['CUDAExecutionProvider', 'CPUExecutionProvider']},
    auto_tagger: {state: 'loaded', requested: 'CUDA', active: 'CPUExecutionProvider', providers: ['CPUExecutionProvider']},
    ...overrides};
}
test('panel and clipboard include every GPU, interpreter, runtime and actual CPU fallback', () => {
  const data = report(), h = harness(data);
  h.ctx.renderDiagnostics(data); h.ctx.copyDiagnostics();
  for (const text of [h.el('diagSections').innerHTML, h.clipboard()]) {
    for (const value of ['GPU 1', 'GPU 2', 'AMD RX 580', 'NVIDIA RTX 3060', '/hub/.venv/bin/python',
      '1.31.0', 'CUDAExecutionProvider', 'Tagger requested', 'Tagger active', 'CPUExecutionProvider',
      'Danbooru uses a separate browser runtime']) assert.ok(text.includes(value), value);
  }
  assert.match(h.clipboard(), /Tagger active:\s+CPUExecutionProvider/);
});
test('idle CUDA selection is never presented as the active provider', () => {
  const h = harness(report({auto_tagger: {state: 'idle', requested: 'CUDA', active: '', providers: []}}));
  h.ctx.copyDiagnostics();
  assert.match(h.clipboard(), /Tagger requested:\s+CUDA/);
  assert.match(h.clipboard(), /Tagger active:\s+No model loaded/);
});
test('missing ONNX, broken ONNX and unloaded modules have explicit states', () => {
  for (const [runtime, message] of [
    [{status: 'missing'}, 'Not installed'],
    [{status: 'error', version: '1.31.0', error: 'libcudnn missing'}, '1.31.0 (error)'],
  ]) {
    const data = report({onnx_runtime: runtime, auto_tagger: {state: 'not_loaded'}}), h = harness(data);
    h.ctx.renderDiagnostics(data); h.ctx.copyDiagnostics();
    assert.ok(h.clipboard().includes(message));
    assert.match(h.clipboard(), /Tagger active:\s+Module not loaded/);
    if (runtime.error) assert.ok(h.clipboard().includes(runtime.error));
  }
});
test('diagnostic values are escaped before insertion into the panel', () => {
  const attack = '<img src=x onerror=bad()>', data = report({gpus: [attack], python_executable: attack,
    onnx_runtime: {status: 'error', error: attack}, auto_tagger: {state: 'error', error: attack}});
  const h = harness(data); h.ctx.renderDiagnostics(data);
  const html = h.el('diagSections').innerHTML;
  assert.ok(!html.includes(attack));
  assert.ok(html.includes('&lt;img src=x onerror=bad()&gt;'));
});
test('older payloads retain the existing GPU display and copy report', () => {
  const data = {gpu: 'Legacy GPU'}, h = harness(data);
  h.ctx.renderDiagnostics(data); h.ctx.copyDiagnostics();
  assert.match(h.el('diagSections').innerHTML, /Legacy GPU/);
  assert.match(h.clipboard(), /GPU:\s+Legacy GPU/);
  assert.ok(!h.clipboard().includes('# ONNX / Auto Tagger'));
});
