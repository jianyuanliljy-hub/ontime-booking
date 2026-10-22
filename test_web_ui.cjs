// 直接执行页面脚本，使用最小 DOM 和本机状态替身；不请求网络、不安装依赖。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function page() {
  const elements = new Map();
  function element() {
    return {
      value: '', textContent: '', hidden: false, disabled: false, checked: false, open: false,
      children: [], listeners: {}, attributes: {},
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute(key, value) { this.attributes[key] = value; },
      addEventListener(name, callback) { this.listeners[name] = callback; },
      replaceChildren(...items) { this.children = items; },
      close() { this.open = false; }, showModal() { this.open = true; }, focus() {},
    };
  }
  const get = id => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: get, createElement: element, createTextNode: text => ({ textContent: text }), addEventListener() {} },
    Date, Intl, AbortSignal, fetch: () => new Promise(() => {}), setTimeout() {}, setInterval() {}, console,
  });
  vm.runInContext(fs.readFileSync(`${__dirname}/web/app.js`, 'utf8'), context);
  const run = code => vm.runInContext(code, context);
  const snapshot = {
    csrf: 'TEST_CSRF', revision: 1, connected: true, verified_at: new Date().toISOString(),
    server_now: new Date().toISOString(), default_date: '2099-09-17',
    slots: ['06:00-07:30', '19:30-21:30'], checked_at: new Date().toISOString(), plan_id: 'plan-a', recoveries: [],
    plan: { use_day: '2099-09-17', slot: '06:00-07:30', release_at: '2099-09-16T07:30:00+08:00',
      current_rows: [], existing_order: null, notice: { hash: 'TEST_NOTICE', text: '测试须知' } },
    job: { id: 'job-a', active: false, kind: 'checking_plan', phase: 'ready', target: { use_day: '2099-09-17', slot: '06:00-07:30' } },
  };
  const setState = value => {
    context.testState = value;
    run('state = testState; initialized = true; $("use-day").value = "2099-09-17"; render();');
  };
  setState(snapshot);
  return { get, run, context, snapshot, setState };
}

test('核对旧日期成功不会把当前场次标成已出票', () => {
  const p = page();
  p.snapshot.job = { id: 'old-order', active: false, kind: 'reconciling', phase: 'confirmed', target: { use_day: '2020-01-01', slot: '06:00-07:30' } };
  p.setState(p.snapshot);
  assert.notEqual(p.get('stock-status').textContent, '已确认');
  assert.equal(p.get('progress-panel').hidden, true);
});

test('没有预检查计划也能显示对应场次的恢复核对结果', () => {
  const p = page();
  p.snapshot.plan = null;
  p.snapshot.job = { ...p.snapshot.job, kind: 'reconciling', phase: 'confirmed' };
  p.setState(p.snapshot);
  assert.equal(p.get('stock-status').textContent, '已确认');
  assert.equal(p.get('primary-button').disabled, true);
});

test('其他页面启动的任务同步实际日期和时段', async () => {
  const p = page();
  const next = { ...p.snapshot, job: { id: 'job-b', active: true, kind: 'reconciling', phase: 'reconciling', target: { use_day: '2020-01-01', slot: '19:30-21:30' } } };
  p.context.fetch = async () => ({ ok: true, json: async () => next });
  await p.run('refresh()');
  assert.equal(p.get('use-day').value, '2020-01-01');
  assert.equal(p.run('slot'), '19:30-21:30');
});

test('清空日期会清空摘要，不再保留原日期', () => {
  const p = page();
  p.get('use-day').value = '';
  p.run('render()');
  assert.equal(p.get('selected-date').textContent, '请选择日期');
  assert.equal(p.get('selected-weekday').textContent, '');
  assert.equal(p.get('primary-button').disabled, true);
});

test('日期超出支持范围时保留可操作页面', () => {
  const p = page();
  p.get('use-day').value = '275761-01-01';
  assert.doesNotThrow(() => p.run('render()'));
  assert.equal(p.get('primary-button').disabled, true);
});

test('确认框打开后预检查过期不能继续提交', () => {
  const p = page();
  p.run('openConfirmation()');
  p.get('accept-notice').checked = true;
  p.snapshot.checked_at = new Date(Date.now() - 601000).toISOString();
  p.setState(p.snapshot);
  assert.equal(p.get('confirm-submit').disabled, true);
});

test('停止操作携带当前任务标识和页面版本', async () => {
  const p = page();
  p.snapshot.job = { ...p.snapshot.job, active: true, kind: 'scheduling', phase: 'waiting', can_cancel: true };
  p.setState(p.snapshot);
  const requests = [];
  p.context.fetch = async (path, options) => {
    if (options.method === 'POST') requests.push(JSON.parse(options.body));
    return { ok: true, json: async () => options.method === 'POST' ? { ok: true } : p.snapshot };
  };
  await p.get('secondary-button').listeners.click();
  assert.equal(requests[0].job_id, 'job-a');
  assert.equal(requests[0].revision, 1);
});

test('订单核对时登录失效能在入口旁提示', () => {
  const p = page();
  p.snapshot.connected = false;
  p.snapshot.job = { ...p.snapshot.job, kind: 'reconciling', phase: 'unknown', result: { requires_login: true }, message: '请更新入口后继续核对' };
  p.setState(p.snapshot);
  assert.equal(p.get('account-error').hidden, false);
  assert.match(p.get('account-error').textContent, /更新入口/);
});

test('页面刷新恢复旧订单结果，不回到另一个预检查日期', async () => {
  const p = page();
  p.snapshot.job = { ...p.snapshot.job, id: 'recovery-job', kind: 'reconciling', phase: 'confirmed',
    target: { use_day: '2020-01-01', slot: '06:00-07:30' } };
  p.context.fetch = async () => ({ ok: true, json: async () => p.snapshot });
  p.run('initialized = false');
  await p.run('refresh()');
  assert.equal(p.get('use-day').value, '2020-01-01');
  assert.equal(p.get('summary-title').textContent, '本次订单核对');
  assert.equal(p.get('stock-status').textContent, '已确认');
  p.get('use-day').value = '2099-09-17';
  p.run('render()');
  assert.notEqual(p.get('stock-status').textContent, '已确认');
  assert.equal(p.get('summary-title').textContent, '你的下一场');
});

test('其他页面已启动同一预约时旧确认框自动关闭', async () => {
  const p = page();
  p.run('openConfirmation()');
  const next = { ...p.snapshot, job: { ...p.snapshot.job, id: 'job-b', active: true, kind: 'scheduling', phase: 'waiting' } };
  p.context.fetch = async () => ({ ok: true, json: async () => next });
  await p.run('refresh()');
  assert.equal(p.get('confirm-dialog').open, false);
  assert.equal(p.get('confirm-submit').disabled, true);
});

test('较早请求的迟到响应不会覆盖新的任务状态', async () => {
  const p = page();
  const requests = [];
  p.context.fetch = () => new Promise(resolve => requests.push(resolve));
  const first = p.run('refresh()');
  const second = p.run('refresh()');
  const next = { ...p.snapshot, revision: 2, job: { ...p.snapshot.job, id: 'job-b', active: true, kind: 'scheduling', phase: 'waiting' } };
  requests[1]({ ok: true, json: async () => next });
  await second;
  requests[0]({ ok: true, json: async () => p.snapshot });
  await first;
  assert.equal(p.run('state.revision'), 2);
  assert.equal(p.get('stock-status').textContent, '预约中');
});

test('检查完成后结果卡直接显示明确余票结论', () => {
  const p = page();
  p.snapshot.plan.current_rows = [{ date: '2099-09-17', slot: '06:00-07:30', remaining: 0 }];
  p.snapshot.job = { ...p.snapshot.job, active: false, kind: 'checking_plan', phase: 'ready', target: { use_day: '2099-09-17', slot: '06:00-07:30' } };
  p.setState(p.snapshot);
  assert.equal(p.get('result-banner').hidden, false);
  assert.equal(p.get('result-title').textContent, '检查完成 · 当前无票');
  assert.match(p.get('result-detail').textContent, /没有查到/);
});

test('查询中显示当前步骤和动态耗时', () => {
  const p = page();
  p.snapshot.job = { ...p.snapshot.job, active: true, kind: 'checking_plan', phase: 'checking_plan',
    started_at: new Date(Date.now() - 4200).toISOString(), values: { step: 'catalog' },
    target: { use_day: '2099-09-17', slot: '06:00-07:30' } };
  p.setState(p.snapshot);
  assert.equal(p.get('result-banner').hidden, false);
  assert.equal(p.get('result-title').textContent, '正在查询这场预约');
  assert.match(p.get('progress-copy').textContent, /健身票/);
  assert.match(p.get('progress-elapsed').textContent, /已运行/);
  assert.match(p.get('result-time').textContent, /已运行/);
});

test('查询完成后结果卡显示本次耗时', () => {
  const p = page();
  p.snapshot.plan.current_rows = [{ date: '2099-09-17', slot: '06:00-07:30', remaining: 0 }];
  p.snapshot.job = { ...p.snapshot.job, active: false, kind: 'checking_plan', phase: 'ready',
    started_at: '2099-09-16T00:00:00+08:00', finished_at: '2099-09-16T00:00:07+08:00',
    target: { use_day: '2099-09-17', slot: '06:00-07:30' } };
  p.setState(p.snapshot);
  assert.match(p.get('result-time').textContent, /本次耗时 7 秒/);
  assert.match(p.get('progress-elapsed').textContent, /本次耗时 7 秒/);
});

test('连接后显示全部时段余票总览', () => {
  const p = page();
  p.snapshot.plan = null;
  p.snapshot.overview = { use_day: '2099-09-17', checked_at: new Date().toISOString(), slots: [
    { slot: '06:00-07:30', remaining: 2, status: 'available' },
    { slot: '19:30-21:30', remaining: 0, status: 'sold' },
  ] };
  p.snapshot.job = { id: 'overview-job', active: false, kind: 'connecting', phase: 'connected', target: null };
  p.setState(p.snapshot);
  assert.equal(p.get('overview-panel').hidden, false);
  assert.equal(p.get('overview-grid').children.length, 2);
  assert.equal(p.get('result-title').textContent, '余票总览已更新 · 9月17日');
});

test('切换到明天后只显示明天的总览，并批量查询该日期', async () => {
  const p = page();
  p.snapshot.plan = null;
  p.snapshot.overview = { use_day: '2099-09-17', checked_at: new Date().toISOString(), slots: [
    { slot: '06:00-07:30', remaining: 2, status: 'available' },
  ] };
  p.snapshot.job = { id: 'overview-job', active: false, kind: 'scanning', phase: 'overview_ready', target: { use_day: '2099-09-17', slot: '' } };
  p.setState(p.snapshot);
  const requests = [];
  let finishPost;
  p.context.fetch = async (path, options) => {
    if (options.method === 'POST') {
      requests.push({ path, body: JSON.parse(options.body) });
      return new Promise(resolve => { finishPost = resolve; });
    }
    return { ok: true, json: async () => p.snapshot };
  };
  const tomorrow = '2099-09-18';
  p.get('use-day').value = tomorrow;
  const changed = p.get('use-day').listeners.change();
  assert.equal(p.get('overview-title').textContent, '余票总览 · 9月18日');
  assert.equal(p.get('overview-grid').children.length, 1);
  assert.equal(p.get('overview-grid').children[0].className, 'overview-loading');
  assert.equal(p.get('result-banner').hidden, true);
  assert.equal(requests[0].path, '/api/overview');
  assert.equal(requests[0].body.use_day, tomorrow);
  assert.equal(requests[0].body.revision, 1);
  p.snapshot.overview = { use_day: tomorrow, checked_at: new Date().toISOString(), slots: [
    { slot: '06:00-07:30', remaining: 0, status: 'sold' },
    { slot: '19:30-21:30', remaining: null, status: 'unknown' },
  ] };
  p.snapshot.job = { id: 'tomorrow-job', active: false, kind: 'scanning', phase: 'overview_ready', target: { use_day: tomorrow, slot: '' } };
  finishPost({ ok: true, json: async () => ({ ok: true }) });
  await changed;
  assert.equal(p.get('overview-grid').children.length, 2);
  assert.equal(p.get('overview-grid').children[1].children[1].textContent, '余量未知');
  assert.equal(p.get('result-title').textContent, '余票总览已更新 · 9月18日');
});

test('明天快捷按钮按北京时间选日期，刷新总览仍使用当前日期', async () => {
  const p = page();
  p.snapshot.plan = null;
  p.snapshot.overview = { use_day: '2099-09-17', checked_at: new Date().toISOString(), slots: [] };
  p.snapshot.job = { id: 'overview-job', active: false, kind: 'scanning', phase: 'overview_ready', target: { use_day: '2099-09-17', slot: '' } };
  p.setState(p.snapshot);
  const expected = p.run('shanghaiDay(1)');
  const requests = [];
  p.context.fetch = async (path, options) => {
    if (options.method === 'POST') requests.push(JSON.parse(options.body));
    return { ok: true, json: async () => options.method === 'POST' ? { ok: true } : p.snapshot };
  };
  await p.get('day-tomorrow').listeners.click();
  assert.equal(p.get('use-day').value, expected);
  assert.equal(p.get('day-tomorrow').attributes['aria-pressed'], 'true');
  await p.get('overview-refresh').listeners.click();
  assert.equal(requests.length, 2);
  assert.equal(requests[0].use_day, expected);
  assert.equal(requests[1].use_day, expected);
});

test('批量查询后仍优先提示同场待核对订单', () => {
  const p = page();
  p.snapshot.overview = { use_day: '2099-09-17', checked_at: new Date().toISOString(), slots: [] };
  p.snapshot.recoveries = [{ use_day: '2099-09-17', slot: '06:00-07:30' }];
  p.snapshot.job = { id: 'overview-job', active: false, kind: 'scanning', phase: 'overview_ready', target: { use_day: '2099-09-17', slot: '' } };
  p.setState(p.snapshot);
  assert.equal(p.get('result-title').textContent, '结果需要核对');
  assert.match(p.get('result-detail').textContent, /不会重复提交/);
  assert.equal(p.get('primary-button').textContent, '继续核对订单');
});

test('快速无票结果不会打开预约确认', () => {
  const p = page();
  p.snapshot.overview = null;
  p.snapshot.plan = { ...p.snapshot.plan, fast_only: true,
    current_rows: [{ date: '2099-09-17', slot: '06:00-07:30', remaining: 0 }] };
  p.snapshot.job = { ...p.snapshot.job, active: false, phase: 'ready', target: { use_day: '2099-09-17', slot: '06:00-07:30' } };
  p.setState(p.snapshot);
  assert.equal(p.get('primary-button').disabled, true);
  assert.equal(p.get('primary-button').textContent, '当前无票');
});

test('有票且尚未到放票时间显示开始抢票', () => {
  const p = page();
  p.snapshot.overview = null;
  p.snapshot.plan = { ...p.snapshot.plan, fast_only: false,
    current_rows: [{ date: '2099-09-17', slot: '06:00-07:30', remaining: 1 }] };
  p.snapshot.job = { ...p.snapshot.job, active: false, phase: 'ready', target: { use_day: '2099-09-17', slot: '06:00-07:30' } };
  p.setState(p.snapshot);
  assert.equal(p.get('primary-button').disabled, false);
  assert.equal(p.get('primary-button').textContent, '开始抢票');
});
