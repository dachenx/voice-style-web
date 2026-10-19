import { expect, test, type Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';

async function project(page: Page, simulated = true) {
  if (simulated) await page.addInitScript(() => localStorage.setItem('voice-adapter', 'simulated-tone'));
  const value = await (await page.request.post('/api/projects', { data: { name: '简洁流程' } })).json();
  const audio = Buffer.alloc(44 + 24000 * 2);
  audio.write('RIFF'); audio.writeUInt32LE(audio.length - 8, 4); audio.write('WAVEfmt ', 8);
  audio.writeUInt32LE(16, 16); audio.writeUInt16LE(1, 20); audio.writeUInt16LE(1, 22);
  audio.writeUInt32LE(24000, 24); audio.writeUInt32LE(48000, 28); audio.writeUInt16LE(2, 32); audio.writeUInt16LE(16, 34);
  audio.write('data', 36); audio.writeUInt32LE(48000, 40);
  for (let i = 0; i < 24000; i++) audio.writeInt16LE(Math.round(4000 * Math.sin(i / 10)), 44 + i * 2);
  const ref = await (await page.request.post(`/api/projects/${value.id}/references?filename=synthetic.wav`,
    { headers: { 'Content-Type': 'application/octet-stream' }, data: audio })).json();
  expect((await page.request.post(`/api/projects/${value.id}/references/${ref.id}/clips`, { data: { start_ms: 0, end_ms: 800 } })).ok()).toBe(true);
  return value.id as string;
}
const generate = (page: Page) => page.getByRole('button', { name: '生成测试音（不朗读）', exact: true });

test('无偏好时真实优先，不可调用不降级，主动选择可记住', async ({ page }) => {
  const id = await project(page, false); let posts = 0;
  page.on('request', r => { if (r.method() === 'POST' && r.url().endsWith('/tasks')) posts++; });
  await page.route('**/api/generation-adapters/*/health', route => route.fulfill({ json: {
    adapter_id: new URL(route.request().url()).pathname.split('/').at(-2), configured: false,
    configuration_valid: false, execution_available: false, model_loaded: false, state: 'unconfigured', error: null,
  } }));
  await page.goto(`/#project=${id}`);
  await expect(page.getByLabel('真实模型状态')).toContainText('模型未配置');
  await expect(page.getByRole('button', { name: '生成配音', exact: true })).toBeDisabled();
  await expect(generate(page)).toHaveCount(0); expect(posts).toBe(0);
  await page.getByText('模型选择与详细设置', { exact: true }).click();
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('simulated-tone');
  await page.reload(); await expect(generate(page)).toBeVisible();
});

test('准备屏障锁住重复和旧任务重试，真实保存冲突保留输入', async ({ page }) => {
  const id = await project(page); let confirmations = 0, posts = 0;
  const failed = { id: randomUUID(), project_id: id, mode: 'full', state: 'failed', phase: 'failed', state_version: 1,
    is_simulated: true, adapter: { id: 'simulated-tone', version: '1' }, completed_paragraphs: 0, total_paragraphs: 1,
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(), retry_of: null, result: null, error: { code: 'test', message: '构造的失败记录' } };
  await page.route(`**/api/projects/${id}/tasks`, route => route.request().method() === 'GET'
    ? route.fulfill({ json: { tasks: [failed], worker: { state: 'ready', error: null } } }) : route.continue());
  let release!: () => void; const barrier = new Promise<void>(resolve => { release = resolve; });
  let started!: () => void; const entered = new Promise<void>(resolve => { started = resolve; });
  await page.route(`**/api/projects/${id}/text`, async route => {
    if (route.request().method() !== 'PUT') return route.continue();
    started(); await barrier; await route.continue();
  });
  page.on('request', r => {
    if (r.method() === 'POST' && r.url().endsWith('/text/confirm')) confirmations++;
    if (r.method() === 'POST' && /\/tasks(?:\/|$)/.test(new URL(r.url()).pathname)) posts++;
  });
  await page.goto(`/#project=${id}`);
  await page.getByLabel('要朗读的文案', { exact: true }).fill('本地修改保留');
  await expect(generate(page)).toBeEnabled(); await generate(page).click(); await entered;
  try {
    await expect(page.getByRole('button', { name: '正在准备并提交…', exact: true })).toBeDisabled();
    await expect(page.getByRole('button', { name: '按原输入重试', exact: true })).toBeDisabled();
    const external = await page.request.put(`/api/projects/${id}/text`, { data: { expected_revision: 0,
      source_language: 'zh-CN', target_language: 'wuhan', register: '', terms: '', paragraphs: [{ id: randomUUID(), original_text: '另窗', draft_text: '另窗', pronunciation_note: '' }] } });
    expect(external.ok()).toBe(true);
  } finally { release(); }
  await expect(page.getByRole('alert', { name: '任务操作错误' })).toContainText('尚未提交');
  await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('本地修改保留');
  expect(confirmations).toBe(0); expect(posts).toBe(0);
});

test('保存成功确认失败仍刷新项目时间及列表顺序', async ({ page }) => {
  const id = await project(page); const other = await project(page);
  await page.route(`**/api/projects/${id}/text/confirm`, route => route.fulfill({ status: 503, json: { error: { message: '确认暂时不可用' } } }));
  await page.goto(`/#project=${id}`);
  await page.getByLabel('要朗读的文案', { exact: true }).fill('已保存尚未确认');
  await expect(generate(page)).toBeEnabled(); await generate(page).click();
  await expect(page.getByRole('alert', { name: '任务操作错误' })).toContainText('尚未提交');
  const updated = await (await page.request.get(`/api/projects/${id}`)).json();
  await expect(page.locator('time').last()).toHaveAttribute('datetime', updated.updated_at);
  expect((await (await page.request.get(`/api/projects/${id}/text`)).json()).confirmation_current).toBe(false);
  await expect(page.locator('a.project-card').first()).toHaveAttribute('href', `#project=${id}`);
  const entries = await (await page.request.get('/api/projects')).json();
  expect(entries.projects.findIndex((p: { id: string }) => p.id === id)).toBeLessThan(entries.projects.findIndex((p: { id: string }) => p.id === other));
});

test('确认响应丢失后重试，不重复快照或丢输入', async ({ page }) => {
  const id = await project(page); let attempts = 0;
  await page.route(`**/api/projects/${id}/text/confirm`, async route => {
    attempts++;
    if (attempts === 1) { await route.fetch(); await route.abort(); } else await route.continue();
  });
  await page.goto(`/#project=${id}`);
  await page.getByLabel('要朗读的文案', { exact: true }).fill('确认失败后仍保留。');
  await expect(generate(page)).toBeEnabled(); await generate(page).click();
  await expect(page.getByRole('alert', { name: '任务操作错误' })).toContainText('尚未提交');
  expect((await (await page.request.get(`/api/projects/${id}/tasks`)).json()).tasks).toHaveLength(0);
  await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('确认失败后仍保留。');
  await expect(page.getByLabel('文案操作状态')).not.toContainText('正在');
  await expect(generate(page)).toBeEnabled(); await generate(page).click();
  await expect(page.getByLabel('模拟任务结果', { exact: true })).toBeVisible({ timeout: 15000 });
  expect((await (await page.request.get(`/api/projects/${id}/text/confirmations`)).json()).confirmations).toHaveLength(1);
});

test('旧多段排序保存确认，取消拖拽不接受外部重排', async ({ page }) => {
  const id = await project(page);
  const paragraphs = [1, 2].map(n => ({ id: randomUUID(), original_text: '原稿' + n, draft_text: '草稿' + n, pronunciation_note: '' }));
  await page.request.put(`/api/projects/${id}/text`, { data: { expected_revision: 0, source_language: 'zh-CN', target_language: 'wuhan', register: '', terms: '', paragraphs } });
  await page.goto(`/#project=${id}`);
  await page.getByText('高级分段编辑与历史', { exact: true }).click();
  const first = page.getByRole('article', { name: '第 1 段', exact: true });
  const second = page.getByRole('article', { name: '第 2 段', exact: true });
  const transfer = await page.evaluateHandle(() => new DataTransfer());
  await first.dispatchEvent('dragstart', { dataTransfer: transfer });
  await first.dispatchEvent('dragend', { dataTransfer: transfer });
  await second.dispatchEvent('drop', { dataTransfer: transfer });
  await expect(first).toHaveAttribute('data-paragraph-id', paragraphs[0].id);
  await first.dispatchEvent('dragstart', { dataTransfer: transfer });
  await second.dispatchEvent('drop', { dataTransfer: transfer });
  await expect(first).toHaveAttribute('data-paragraph-id', paragraphs[1].id);
  await second.dispatchEvent('dragstart', { dataTransfer: transfer });
  await first.dispatchEvent('drop', { dataTransfer: transfer });
  await expect(first).toHaveAttribute('data-paragraph-id', paragraphs[0].id);
  await first.dispatchEvent('dragstart', { dataTransfer: transfer });
  await second.dispatchEvent('drop', { dataTransfer: transfer });
  await page.getByRole('button', { name: '保存并确认文案与顺序', exact: true }).click();
  await expect(page.getByLabel('文案操作状态')).toContainText('已保存并确认');
  const saved = await (await page.request.get(`/api/projects/${id}/text`)).json();
  expect(saved.paragraphs).toEqual([...paragraphs].reverse()); expect(saved.confirmation_current).toBe(true);
});

test('默认单框一键生成、播放下载与拼接引导', async ({ page }) => {
  const id = await project(page);
  await page.goto(`/#project=${id}`);
  await expect(page.getByLabel('普通话原稿', { exact: true })).not.toBeVisible();
  await expect(page.getByRole('button', { name: '确认当前文案', exact: true })).not.toBeVisible();
  await page.getByLabel('要朗读的文案', { exact: true }).fill('这是一条测试文案。');
  await expect(generate(page)).toBeEnabled();
  await generate(page).click();
  await expect(page.getByLabel('模拟任务结果', { exact: true })).toBeVisible({ timeout: 15000 });
  await expect(page.getByRole('link', { name: '下载模拟音频', exact: true })).toBeVisible();
  const doc = await (await page.request.get(`/api/projects/${id}/text`)).json();
  expect(doc.confirmation_current).toBe(true);
  expect(doc.paragraphs[0].original_text).toBe('这是一条测试文案。');
  expect(doc.latest_confirmation.paragraphs[0].draft_text).toBe('这是一条测试文案。');
  const exp = page.getByRole('region', { name: '拼接与导出', exact: true });
  await expect(exp.getByLabel('拼接操作步骤')).toContainText('调整顺序');
  await expect(exp).toContainText('第 1 段');
  await exp.getByRole('button', { name: '选择或更换拼接音频' }).click();
  await page.getByRole('button', { name: '选用此版本', exact: true }).click();
  await expect(exp.getByRole('button', { name: '导出整篇 WAV', exact: true })).toBeEnabled();
  await exp.getByRole('button', { name: '导出整篇 WAV', exact: true }).click();
  await expect(exp.getByLabel('试听拼接结果')).toBeVisible();
  const audio = exp.getByLabel('试听拼接结果');
  await audio.evaluate(async element => { await (element as HTMLAudioElement).play(); });
  await expect.poll(() => audio.evaluate(element => (element as HTMLAudioElement).currentTime)).toBeGreaterThan(0);
  expect(await audio.evaluate(element => Number.isFinite((element as HTMLAudioElement).duration))).toBe(true);
  await expect(exp.getByRole('link', { name: '下载 WAV', exact: true })).toBeVisible();
});

test('保存失败不确认或入队，重试保持输入', async ({ page }) => {
  const id = await project(page);
  let confirmations = 0, tasks = 0;
  page.on('request', r => {
    if (r.method() === 'POST' && r.url().endsWith('/text/confirm')) confirmations++;
    if (r.method() === 'POST' && r.url().endsWith('/tasks')) tasks++;
  });
  await page.route(`**/api/projects/${id}/text`, async route => {
    if (route.request().method() === 'PUT') await route.abort(); else await route.continue();
  });
  await page.goto(`/#project=${id}`);
  await page.getByLabel('要朗读的文案', { exact: true }).fill('失败仍保留。');
  await expect(generate(page)).toBeEnabled(); await generate(page).click();
  await expect(page.getByRole('alert', { name: '任务操作错误' })).toContainText('尚未提交');
  expect(confirmations).toBe(0); expect(tasks).toBe(0);
  await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('失败仍保留。');
  await page.unroute(`**/api/projects/${id}/text`);
  await generate(page).click();
  await expect(page.getByLabel('模拟任务结果', { exact: true })).toBeVisible({ timeout: 15000 });
  expect(tasks).toBe(1);
});

test('旧多段只修改当前草稿，身份原稿备注和顺序保留', async ({ page }) => {
  const id = await project(page);
  const paragraphs = [1, 2].map(n => ({ id: randomUUID(), original_text: '旧原稿' + n, draft_text: '旧草稿' + n, pronunciation_note: '旧备注' + n }));
  await page.request.put(`/api/projects/${id}/text`, { data: { expected_revision: 0, source_language: 'zh-CN', target_language: 'wuhan', register: '旧语体', terms: '旧术语', paragraphs } });
  await page.goto(`/#project=${id}`);
  await expect(page.getByText('已保留原有 2 段及顺序。')).toBeVisible();
  await page.getByLabel('编辑哪一段', { exact: false }).selectOption(paragraphs[1].id);
  await page.getByLabel('要朗读的文案', { exact: true }).fill('第二段新草稿');
  await expect(generate(page)).toBeEnabled(); await generate(page).click();
  await expect(page.getByLabel('模拟任务结果', { exact: true })).toBeVisible({ timeout: 15000 });
  const saved = await (await page.request.get(`/api/projects/${id}/text`)).json();
  expect(saved.paragraphs).toEqual([paragraphs[0], { ...paragraphs[1], draft_text: '第二段新草稿' }]);
  expect(saved.register).toBe('旧语体'); expect(saved.terms).toBe('旧术语');
});

test('任务响应丢失不重复提交，恢复沿用同一输入', async ({ page }) => {
  const id = await project(page); const bodies: string[] = [];
  await page.route(`**/api/projects/${id}/tasks`, async route => {
    if (route.request().method() !== 'POST') return route.continue();
    bodies.push(route.request().postData()!);
    if (bodies.length === 1) { await route.fetch(); await route.abort(); } else await route.continue();
  });
  await page.goto(`/#project=${id}`);
  await page.getByLabel('要朗读的文案', { exact: true }).fill('只生成一次');
  await expect(generate(page)).toBeEnabled(); await generate(page).click();
  await expect(page.getByRole('button', { name: '核对并重试上次提交', exact: true })).toBeVisible();
  await expect(generate(page)).toBeDisabled();
  await page.getByRole('button', { name: '核对并重试上次提交', exact: true }).click();
  await expect(page.getByRole('button', { name: '核对并重试上次提交', exact: true })).toHaveCount(0);
  expect(bodies).toHaveLength(2); expect(bodies[0]).toBe(bodies[1]);
  expect((await (await page.request.get(`/api/projects/${id}/tasks`)).json()).tasks).toHaveLength(1);
});
