import { expect, test, type Page } from './advancedFixture';
import { randomUUID } from 'node:crypto';

async function ready(page: Page) {
  const project = await (await page.request.post('/api/projects', { data: { name: '删除任务测试' } })).json();
  const base = `/api/projects/${project.id}`;
  const audio = Buffer.alloc(48044);
  audio.write('RIFF'); audio.writeUInt32LE(audio.length - 8, 4); audio.write('WAVEfmt ', 8);
  audio.writeUInt32LE(16, 16); audio.writeUInt16LE(1, 20); audio.writeUInt16LE(1, 22);
  audio.writeUInt32LE(24000, 24); audio.writeUInt32LE(48000, 28); audio.writeUInt16LE(2, 32); audio.writeUInt16LE(16, 34);
  audio.write('data', 36); audio.writeUInt32LE(48000, 40);
  for (let i = 0; i < 24000; i++) audio.writeInt16LE(Math.round(4000 * Math.sin(i / 10)), 44 + i * 2);
  const reference = await (await page.request.post(`${base}/references?filename=synthetic.wav`, {
    headers: { 'Content-Type': 'application/octet-stream' }, data: audio,
  })).json();
  const clip = await (await page.request.post(`${base}/references/${reference.id}/clips`, { data: { start_ms: 0, end_ms: 800 } })).json();
  const document = await (await page.request.put(`${base}/text`, { data: { expected_revision: 0, source_language: 'zh-CN', target_language: 'wuhan', register: '', terms: '',
    paragraphs: [{ id: randomUUID(), original_text: '原稿', draft_text: '测试朗读稿', pronunciation_note: '' }] } })).json();
  const confirmed = await (await page.request.post(`${base}/text/confirm`, { data: { expected_revision: document.revision, acknowledged: true } })).json();
  const response = await page.request.post(`${base}/tasks`, { data: { request_id: randomUUID(), expected_revision: confirmed.revision,
    expected_confirmation_id: confirmed.latest_confirmation.id, expected_clip_id: clip.id, adapter_id: 'simulated-tone', mode: 'full', paragraph_id: null, parameters: {} } });
  expect(response.ok()).toBe(true);
  const task = await response.json();
  await expect.poll(async () => (await (await page.request.get(`${base}/tasks/${task.id}`)).json()).state).toBe('succeeded');
  await page.goto(`/#project=${project.id}`);
  const card = page.locator(`[data-task-id="${task.id}"]`);
  await expect(card.getByRole('button', { name: '删除任务及音频' })).toBeEnabled();
  return { base, task, project, card };
}

test('真实删除二次确认取消、快照清空、版本移除和刷新保持', async ({ page }) => {
  const { base, task, card } = await ready(page);
  await card.getByRole('button', { name: '查看任务快照' }).click();
  await expect(page.getByRole('region', { name: '任务输入快照' })).toBeVisible();
  await card.getByRole('button', { name: '删除任务及音频' }).click();
  await page.getByRole('button', { name: '取消删除', exact: true }).click();
  expect((await page.request.get(`${base}/tasks/${task.id}/audio`)).ok()).toBe(true);
  await card.getByRole('button', { name: '删除任务及音频' }).click();
  await page.getByRole('button', { name: '确认永久删除任务' }).click();
  await expect(page.getByLabel('任务操作状态')).toContainText('已永久删除');
  await expect(card).toHaveCount(0);
  await expect(page.getByRole('region', { name: '任务输入快照' })).toHaveCount(0);
  expect((await page.request.get(`${base}/tasks/${task.id}/audio`)).status()).toBe(410);
  const versions = await (await page.request.get(`${base}/paragraph-versions?adapter_id=simulated-tone`)).json();
  expect(versions.paragraphs[0].versions).toEqual([]);
  await page.getByRole('button', { name: '完成', exact: true }).click();
  await expect(page.getByRole('heading', { name: '任务记录', exact: true })).toBeFocused();
  await page.reload(); await expect(page.getByText('尚无生成任务。')).toBeVisible();
  await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('测试朗读稿');
});

test('已选用阻止删除并保留播放器和记录', async ({ page }) => {
  const { base, task, card } = await ready(page);
  const catalogue = await (await page.request.get(`${base}/paragraph-versions?adapter_id=simulated-tone`)).json();
  const paragraph = catalogue.paragraphs[0];
  expect((await page.request.put(`${base}/paragraphs/${paragraph.id}/selection`, { data: { version_id: paragraph.versions[0].id, expected_revision: 0,
    adapter_id: 'simulated-tone', comparison_token: catalogue.comparison_token, acknowledge_mismatch: false } })).ok()).toBe(true);
  await card.getByRole('button', { name: '删除任务及音频' }).click();
  await page.getByRole('button', { name: '确认永久删除任务' }).click();
  await expect(page.getByLabel('任务操作错误')).toContainText('选用');
  await expect(card).toBeVisible();
  expect((await page.request.get(`${base}/tasks/${task.id}/audio`)).ok()).toBe(true);
});

test('跨窗口墓碑不被迟到提交复活，后续轮询不能掩盖结果', async ({ page }) => {
  const { base, project } = await ready(page);
  let release!: () => void; const barrier = new Promise<void>(resolve => { release = resolve; });
  let entered!: (taskId: string) => void; const started = new Promise<string>(resolve => { entered = resolve; });
  let handled!: () => void; const done = new Promise<void>(resolve => { handled = resolve; });
  let freezeReads = false;
  await page.route(`**${base}/tasks`, async route => {
    if (route.request().method() !== 'POST') return freezeReads ? route.abort() : route.continue();
    const response = await route.fetch();
    const task = await response.json();
    entered(task.id); await barrier;
    await route.fulfill({ response }); handled();
  });
  await page.getByRole('button', { name: '生成测试音（不朗读）', exact: true }).click();
  const id = await started;
  await expect.poll(async () => (await (await page.request.get(`${base}/tasks/${id}`)).json()).state).toBe('succeeded');
  expect((await page.request.delete(`${base}/tasks/${id}`, { data: { acknowledged: true } })).ok()).toBe(true);
  // Observe a GET containing this tombstone and wait for the UI's polling render.
  const response = page.waitForResponse(async r => r.url().endsWith(`${base}/tasks`) && r.request().method() === 'GET'
    && (await r.json()).deletions.some((d: { task_id: string }) => d.task_id === id));
  await page.getByRole('button', { name: '刷新任务', exact: true }).click(); await response;
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  freezeReads = true;
  release(); await done;
  await expect(page.getByLabel('任务操作状态')).toContainText('已保存到本机队列');
  await expect(page.locator(`[data-task-id="${id}"]`)).toHaveCount(0);
  expect(await page.evaluate(projectId => sessionStorage.getItem(`voice-task-pending:${projectId}`), project.id)).toBeNull();
});

test('文件待清理独立显示并可刷新恢复与同ID重试', async ({ page }) => {
  const { base, task, card } = await ready(page);
  let pending = true;
  await page.route(`**${base}/tasks/${task.id}`, async route => {
    if (route.request().method() !== 'DELETE') return route.continue();
    const response = await route.fetch();
    expect(response.ok()).toBe(true);
    if (pending) return route.fulfill({ status: 503, json: { error: { code: 'task_cleanup_pending', message: '记录已删除，文件清理待重试。' } } });
    await route.fulfill({ response });
  });
  await page.route(`**${base}/tasks`, async route => {
    const response = await route.fetch(); const data = await response.json();
    if (pending) data.deletions = data.deletions.map((item: { task_id: string; completed: number }) => item.task_id === task.id ? { ...item, completed: 0 } : item);
    await route.fulfill({ json: data });
  });
  await card.getByRole('button', { name: '删除任务及音频' }).click();
  await page.getByRole('button', { name: '确认永久删除任务' }).click();
  await expect(page.getByRole('dialog', { name: '确认删除任务' }).getByRole('alert')).toBeInViewport();
  await page.getByRole('button', { name: '关闭提示' }).click();
  await expect(page.getByLabel('任务文件待清理')).toBeVisible();
  await expect(card).toHaveCount(0);
  await page.reload(); await expect(page.getByLabel('任务文件待清理')).toBeVisible();
  pending = false;
  await page.getByRole('button', { name: '重试清理文件' }).click();
  await expect(page.getByLabel('任务操作状态')).toContainText('已永久删除');
  await expect(page.getByLabel('任务文件待清理')).toHaveCount(0);
});

test('未知删除保留重试入口，服务端确认后撤掉记录', async ({ page }) => {
  const { base, task, card } = await ready(page);
  await page.route(`**${base}/tasks/${task.id}`, route => route.request().method() === 'DELETE' ? route.abort() : route.continue());
  await card.getByRole('button', { name: '删除任务及音频' }).click();
  await page.getByRole('button', { name: '确认永久删除任务' }).click();
  await page.getByRole('button', { name: '关闭提示' }).click();
  await expect(page.getByLabel('任务删除待核对')).toBeVisible();
  await expect(page.getByRole('heading', { name: '任务记录', exact: true })).toBeFocused();
  await page.reload();
  await expect(page.getByLabel('任务删除待核对')).toBeVisible();
  await page.unroute(`**${base}/tasks/${task.id}`);
  await page.getByRole('button', { name: '核对并重试删除' }).click();
  await expect(page.getByLabel('任务操作状态')).toContainText('已永久删除');
  await expect(card).toHaveCount(0);
});

test('另一任务待清理不能覆盖当前未知删除意图', async ({ page }) => {
  const { base, project, task, card } = await ready(page);
  const other = { task_id: randomUUID(), completed: 0, created_at: new Date().toISOString() };
  await page.route(`**${base}/tasks`, async route => {
    const response = await route.fetch(); const data = await response.json();
    data.deletions.push(other);
    await route.fulfill({ json: data });
  });
  await page.getByRole('button', { name: '刷新任务', exact: true }).click();
  await expect(page.getByRole('button', { name: '重试清理文件' })).toBeEnabled();
  await page.route(`**${base}/tasks/${task.id}`, route => route.request().method() === 'DELETE' ? route.abort() : route.continue());
  await card.getByRole('button', { name: '删除任务及音频' }).click();
  await page.getByRole('button', { name: '确认永久删除任务' }).click();
  await page.getByRole('button', { name: '关闭提示' }).click();
  await expect(page.getByLabel('任务删除待核对')).toContainText(task.id);
  await expect(page.getByRole('button', { name: '重试清理文件' })).toBeDisabled();
  expect(JSON.parse((await page.evaluate(id => sessionStorage.getItem(`voice-task-deletion:${id}`), project.id))!)).toMatchObject({ task_id: task.id });
  await page.reload();
  await expect(page.getByRole('button', { name: '重试清理文件' })).toBeDisabled();
  await expect(page.getByLabel('任务删除待核对')).toContainText(task.id);
});

test('长列表底部首击确认就在视口内，安全焦点Escape及失败反馈', async ({ page }) => {
  await page.setViewportSize({ width: page.viewportSize()!.width, height: 665 });
  const { base, task, card } = await ready(page);
  const original = await (await page.request.get(`${base}/tasks/${task.id}`)).json();
  const many = Array.from({ length: 20 }, (_, index) => ({ ...original, id: randomUUID(), created_at: new Date(Date.now() + 1000 * (index + 1)).toISOString() }));
  await page.route(`**${base}/tasks`, route => route.fulfill({ json: { tasks: [...many, original], deletions: [], worker: { state: 'idle' } } }));
  await page.getByRole('button', { name: '刷新任务', exact: true }).click();
  await expect(page.locator('[data-task-id]')).toHaveCount(21);
  let deletes = 0;
  await page.route(`**${base}/tasks/${task.id}`, route => {
    if (route.request().method() !== 'DELETE') return route.continue();
    deletes++;
    return route.fulfill({ status: 409, json: { error: { code: 'task_selected', message: '该任务音频已被选用，请先改选其他版本。' } } });
  });
  const trigger = card.getByRole('button', { name: '删除任务及音频' });
  await trigger.click();
  const dialog = page.getByRole('dialog', { name: '确认删除任务' });
  const cancel = dialog.getByRole('button', { name: '取消删除', exact: true });
  const confirm = dialog.getByRole('button', { name: '确认永久删除任务' });
  await expect(dialog).toBeInViewport();
  await expect(cancel).toBeInViewport(); await expect(confirm).toBeInViewport();
  await expect(cancel).toBeFocused();
  expect(deletes).toBe(0);
  await page.keyboard.press('Shift+Tab'); await expect(confirm).toBeFocused();
  await page.keyboard.press('Tab'); await expect(cancel).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0); await expect(trigger).toBeFocused();
  expect(deletes).toBe(0);
  await trigger.click(); await cancel.click();
  await expect(trigger).toBeFocused(); expect(deletes).toBe(0);
  await trigger.click(); await confirm.click();
  await expect(dialog.getByRole('alert')).toBeInViewport();
  await expect(dialog.getByRole('alert')).toContainText('已被选用');
  expect(deletes).toBe(1);
  await dialog.getByRole('button', { name: '关闭提示' }).click();
  await expect(trigger).toBeFocused();
});

test('删除在途禁止Escape和重复确认，完成后可关闭', async ({ page }) => {
  const { base, task, card } = await ready(page);
  let release!: () => void; const barrier = new Promise<void>(resolve => { release = resolve; });
  let entered!: () => void; const start = new Promise<void>(resolve => { entered = resolve; });
  let deletes = 0;
  await page.route(`**${base}/tasks/${task.id}`, async route => {
    if (route.request().method() !== 'DELETE') return route.continue();
    deletes++;
    const response = await route.fetch(); entered(); await barrier;
    await route.fulfill({ response });
  });
  await card.getByRole('button', { name: '删除任务及音频' }).click();
  await page.getByRole('button', { name: '确认永久删除任务' }).click(); await start;
  const dialog = page.getByRole('dialog', { name: '确认删除任务' });
  await expect(dialog.getByRole('button', { name: '正在删除…' })).toBeDisabled();
  await expect(dialog.getByRole('button', { name: '取消删除' })).toBeDisabled();
  await page.keyboard.press('Escape'); await page.keyboard.press('Enter');
  await expect(dialog).toBeInViewport(); expect(deletes).toBe(1);
  release();
  await expect(dialog.getByLabel('任务操作状态')).toContainText('已永久删除');
  await dialog.getByRole('button', { name: '完成', exact: true }).click();
  await expect(dialog).toHaveCount(0); expect(deletes).toBe(1);
});
