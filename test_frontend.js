// 前端纯逻辑回归：Node 内置 assert/vm，无 npm 依赖。
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, 'web', 'index.html'), 'utf8');
const match = html.match(/<script>([\s\S]*?)<\/script>/);
assert(match, '找不到前端 script');

// 30 行迷你 DOM：够运行筛选/加载方案这些会被真实界面触发的分支。
const elements = {};
function fakeEl(id) {
  return elements[id] || (elements[id] = {
    id, value: '', checked: false, textContent: '', innerHTML: '',
    style: {}, dataset: {},
    classList: {add() {}, remove() {}, toggle() {}},
    addEventListener() {},
    querySelectorAll() { return []; },
    appendChild() {},
    insertAdjacentElement() {},
  });
}
const context = {
  console,
  document: {
    addEventListener() {},
    getElementById: fakeEl,
    querySelectorAll() { return []; },
    createElement() { return fakeEl('__new_' + Math.random()); },
    createDocumentFragment() { return fakeEl('__frag'); },
  },
  window: {},
  URLSearchParams,
  setTimeout,
  clearTimeout,
  fetch: async () => { throw new Error('测试中不应发起网络请求'); },
};
vm.createContext(context);
vm.runInContext(match[1], context, {filename: 'index.html'});
const run = code => vm.runInContext(code, context);

assert.strictEqual(run("nameError('正常.txt')"), '');
assert(run("nameError('CON.txt')").includes('保留名'));
assert(run("nameError('COM¹.txt')").includes('保留名'));
assert(run("nameError('报告.')").includes('结尾'));
assert(run("nameError(' 开头空格.txt')").includes('开头'));
assert(run("nameError('a:b.txt')").includes('非法字符'));
assert(run("nameError('😀'.repeat(128))").includes('过长'));

assert.deepStrictEqual(Array.from(run("splitPair('\\\"a,b.txt\\\",\\\"b\\\"\\\"c.txt\\\"')")),
                       ['a,b.txt', 'b"c.txt']);
assert.strictEqual(run("withExt('b.doc', '.docx')"), 'b.docx');
assert.strictEqual(run("withExt('b', '.docx')"), 'b.docx');
assert.strictEqual(run("withExt('2026.10.29 会议', '.docx')"), '2026.10.29 会议.docx');
assert.strictEqual(run("withExt('报告 v1.2', '.docx')"), '报告 v1.2.docx');
assert.strictEqual(run("withExt('报告.pdf', '.docx')"), '报告.docx');
assert.strictEqual(run("parsePaths('D:\\\\dir\\\\a;b.txt')[0]"), 'D:\\dir\\a;b.txt');
assert.strictEqual(run("csvCell(' =SUM(A1:A2)')"), '"\' =SUM(A1:A2)"');
assert.strictEqual(run("csvCell('a\"b')"), '"a""b"');

// 执行范围：所有 status=ok 且勾选的条目都参与，不再被 800 行静默截断。
run(`S.rows = Array.from({length: 901}, (_, i) => ({
  id: String(i), path: 'p' + i, old_name: 'a' + i, new_name: 'b' + i,
  status: 'ok', selected: true, dir: 'D:'
})); S.rows[0].status = 'conflict';`);
assert.strictEqual(run('executableRows().length'), 900);

// 筛选后全选只能选中可见行。
run(`S.rows = [
 {id:'1', path:'1', old_name:'报告A.txt', new_name:'A.txt', status:'ok', selected:false},
 {id:'2', path:'2', old_name:'报告B.txt', new_name:'B.txt', status:'ok', selected:false},
 {id:'3', path:'3', old_name:'其他.txt', new_name:'C.txt', status:'ok', selected:false}
]; refreshRenderedRows=()=>{}; updateStatFromRows=()=>{};`);
fakeEl('kw').value = '报告';
run('checkAll(true)');
assert.strictEqual(run("S.rows.map(r=>r.selected).join(',')"), 'true,true,false');
fakeEl('kw').value = '';

// 方案 enabled:{} 必须保持“全关”，不能被默认值重新打开。
run(`S.state = {default_enabled:{a:true,b:true}, default_params:{},
  rules:[{id:'a'},{id:'b'}]};
S.presets = [{name:'全关', enabled:{}, params:{}}];
S.enabled = {a:true,b:true};
renderRules=()=>{}; schedulePreview=()=>{}; toast=()=>{};`);
fakeEl('presetSel').value = '全关';
run('loadPreset()');
assert.strictEqual(run('S.enabled.a'), false);
assert.strictEqual(run('S.enabled.b'), false);

// 手工编辑校验：修回原名必须 unchanged 且不勾选；同批重名必须 conflict。
run(`S.rows = [
 {id:'1', old_name:'a.txt', new_name:'a.txt', ext:'.txt', dir:'D:', status:'ok', selected:true},
 {id:'2', old_name:'b.txt', new_name:'x.txt', ext:'.txt', dir:'D:', status:'ok', selected:true}
]; revalidateRows(new Set());`);
assert.strictEqual(run("S.rows[0].status"), 'unchanged');
assert.strictEqual(run("S.rows[0].selected"), false);
run(`S.rows[1].new_name='a.txt'; revalidateRows(new Set([S.rows[1]]));`);
assert.strictEqual(run("S.rows[1].status"), 'conflict');
run(`S.rows[0].new_name='c.txt'; S.rows[1].new_name='c.txt'; revalidateRows(new Set());`);
assert.strictEqual(run("S.rows.every(r => r.status === 'conflict')"), true);

console.log('frontend tests: OK');
