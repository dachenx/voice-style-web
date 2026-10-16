import { expect, test, type Page } from '@playwright/test';
import type { TextDocument } from '../src/TextPanel';

async function create(page: Page) {
  const response = await page.request.post('/api/projects', { data: { name: '文案验收项目' } });
  const project = await response.json() as { id: string };
  await page.goto(`/#project=${project.id}`);
  await expect(page.getByRole('button', { name: '添加段落' })).toBeVisible();
  return project.id;
}
async function fill(page: Page) {
  await page.getByRole('button', { name: '添加段落' }).click();
  await page.getByLabel('普通话原稿', { exact: true }).fill('今天我们去汉口。');
  await page.getByLabel('武汉话草稿', { exact: true }).fill('今天我们克汉口。');
  await page.getByLabel('读音备注', { exact: true }).fill('汉口保持地名读音');
}
async function save(page: Page) {
  await page.getByRole('button', { name: '保存文案', exact: true }).click();
  await expect(page.getByRole('status', { name: '文案操作状态' })).toContainText('已保存');
}
async function confirm(page: Page) {
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: '确认当前文案', exact: true }).click();
  await expect(page.getByText('文案已准备好，可在下方提交模拟任务；真实语音模型尚未接入。')).toBeVisible();
}

test('保存刷新、人工确认、编辑失效及历史快照保持不变', async ({ page }) => {
  const id = await create(page);
  await expect(page.getByRole('button', { name: '自动改写（暂不可用）' })).toBeDisabled();
  await fill(page);
  await expect(page.getByRole('button', { name: '确认当前文案', exact: true })).toBeDisabled();
  await save(page);
  await page.reload();
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveValue('今天我们克汉口。');
  await confirm(page);
  const initial = await (await page.request.get(`/api/projects/${id}/text`)).json() as TextDocument;
  await page.getByLabel('武汉话草稿', { exact: true }).fill('明天我们克汉口。');
  await expect(page.getByRole('checkbox')).not.toBeChecked();
  await expect(page.getByText('文案已准备好，可在下方提交模拟任务；真实语音模型尚未接入。')).not.toBeVisible();
  await expect(page.getByRole('button', { name: '确认当前文案', exact: true })).toBeDisabled();
  await save(page);
  await confirm(page);
  await page.getByRole('button', { name: '查看确认历史', exact: true }).click();
  await page.getByRole('button', { name: /查看确认版本 1 ·/ }).click();
  await expect(page.getByRole('region', { name: '确认快照' })).toContainText('今天我们克汉口。');
  expect(await (await page.request.get(`/api/projects/${id}/text/confirmations/${initial.latest_confirmation!.id}`)).json()).toEqual(initial.latest_confirmation);
  let release!: () => void;
  let captured!: () => void;
  let delivered!: () => void;
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  const started = new Promise<void>((resolve) => { captured = resolve; });
  const finished = new Promise<void>((resolve) => { delivered = resolve; });
  await page.route(`**/api/projects/${id}/text/confirmations/${initial.latest_confirmation!.id}`, async (route) => {
    const response = await route.fetch(); captured(); await waiting; await route.fulfill({ response }); delivered();
  });
  await page.getByRole('button', { name: /查看确认版本 1 ·/ }).click();
  await started;
  await page.getByRole('button', { name: /查看确认版本 2 ·/ }).click();
  await expect(page.getByRole('region', { name: '确认快照' })).toContainText('明天我们克汉口。');
  release(); await finished;
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.getByRole('region', { name: '确认快照' })).toContainText('明天我们克汉口。');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('段落 UUID、增删重排和复制原稿按明确操作保存', async ({ page }) => {
  const id = await create(page);
  await fill(page);
  const first = await page.getByRole('article', { name: '第 1 段', exact: true }).getAttribute('data-paragraph-id');
  await page.getByRole('button', { name: '添加段落' }).click();
  const second = page.getByRole('article', { name: '第 2 段', exact: true });
  await second.getByLabel('普通话原稿').fill('第二段');
  await second.getByRole('button', { name: '复制原稿到草稿' }).click();
  await expect(second.getByLabel('武汉话草稿')).toHaveValue('第二段');
  await second.getByRole('button', { name: '上移' }).click();
  await save(page);
  const stored = await (await page.request.get(`/api/projects/${id}/text`)).json() as TextDocument;
  expect(stored.paragraphs[1].id).toBe(first);
  await page.reload();
  const restored = page.getByRole('article', { name: '第 2 段', exact: true });
  await expect(restored).toHaveAttribute('data-paragraph-id', first!);
  await restored.getByRole('button', { name: '删除段落' }).click();
  await save(page);
  expect(((await (await page.request.get(`/api/projects/${id}/text`)).json()) as TextDocument).paragraphs.map((p) => p.id)).toEqual([stored.paragraphs[0].id]);
});

test('保存网络失败保留本地输入；真实双窗口版本冲突显式恢复', async ({ page, context }) => {
  const id = await create(page);
  await fill(page);
  await page.route(`**/api/projects/${id}/text`, async (route) => {
    if (route.request().method() === 'PUT') await route.abort(); else await route.continue();
  });
  await page.getByRole('button', { name: '保存文案', exact: true }).click();
  await expect(page.getByRole('region', { name: '文案工作台' }).getByRole('alert')).toContainText('本地输入已保留');
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveValue('今天我们克汉口。');
  await page.unroute(`**/api/projects/${id}/text`);
  await save(page);
  const other = await context.newPage();
  await other.goto(page.url());
  await expect(other.getByLabel('武汉话草稿', { exact: true })).toHaveValue('今天我们克汉口。');
  await page.getByLabel('武汉话草稿', { exact: true }).fill('窗口一的新稿');
  await save(page);
  await other.getByLabel('武汉话草稿', { exact: true }).fill('窗口二保留的本地稿');
  await other.getByRole('button', { name: '保存文案', exact: true }).click();
  await expect(other.getByRole('region', { name: '文案工作台' }).getByRole('alert')).toContainText('本地输入已保留');
  await expect(other.getByLabel('武汉话草稿', { exact: true })).toHaveValue('窗口二保留的本地稿');
  other.once('dialog', (dialog) => dialog.dismiss());
  await other.getByRole('button', { name: '重新载入服务器版本', exact: true }).click();
  await expect(other.getByLabel('武汉话草稿', { exact: true })).toHaveValue('窗口二保留的本地稿');
  other.once('dialog', (dialog) => dialog.accept());
  await other.getByRole('button', { name: '重新载入服务器版本', exact: true }).click();
  await expect(other.getByLabel('武汉话草稿', { exact: true })).toHaveValue('窗口一的新稿');
  await other.close();
});

test('切换项目忽略迟到保存响应，并恢复本窗口未保存输入', async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()); });
  const id = await create(page);
  await fill(page);
  await save(page);
  await page.getByLabel('武汉话草稿', { exact: true }).fill('原项目未保存输入');
  const next = await (await page.request.post('/api/projects', { data: { name: '另一文案项目' } })).json() as { id: string };
  await page.evaluate((value) => { location.hash = `project=${value}`; }, next.id);
  await expect(page.getByRole('button', { name: '添加段落' })).toBeVisible();
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveCount(0);
  await page.evaluate((value) => { location.hash = `project=${value}`; }, id);
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveValue('原项目未保存输入');
  await expect(page.getByRole('region', { name: '参考音频管理' })).toHaveCount(1);
  await expect(page.getByRole('region', { name: '文案工作台' })).toHaveCount(1);
  let release!: () => void;
  let captured!: () => void;
  let delivered!: () => void;
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  const started = new Promise<void>((resolve) => { captured = resolve; });
  const finished = new Promise<void>((resolve) => { delivered = resolve; });
  await page.route(`**/api/projects/${id}/text`, async (route) => {
    if (route.request().method() !== 'PUT') { await route.continue(); return; }
    const response = await route.fetch(); captured(); await waiting; await route.fulfill({ response }); delivered();
  });
  await page.getByRole('button', { name: '保存文案', exact: true }).click();
  await started;
  await page.evaluate((value) => { location.hash = `project=${value}`; }, next.id);
  await expect(page.getByRole('button', { name: '添加段落' })).toBeVisible();
  await page.getByRole('button', { name: '添加段落' }).click();
  await page.getByLabel('武汉话草稿', { exact: true }).fill('新项目输入');
  release(); await finished;
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.getByRole('status', { name: '文案操作状态' })).not.toContainText('已保存');
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveValue('新项目输入');
  await expect(page.getByRole('region', { name: '参考音频管理' })).toHaveCount(1);
  await expect(page.getByRole('region', { name: '文案工作台' })).toHaveCount(1);
  expect(consoleErrors).toEqual([]);
});

test('离开未保存项目到干净项目及首页，取消刷新后仍可恢复输入', async ({ page }) => {
  const id = await create(page);
  await fill(page);
  const next = await (await page.request.post('/api/projects', { data: { name: '干净项目' } })).json() as { id: string };
  await page.evaluate((value) => { location.hash = `project=${value}`; }, next.id);
  await expect(page.getByRole('button', { name: '添加段落' })).toBeVisible();
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveCount(0);
  async function cancelReload() {
    const dialog = page.waitForEvent('dialog');
    await page.evaluate(() => { setTimeout(() => location.reload(), 0); });
    const prompt = await dialog;
    expect(prompt.type()).toBe('beforeunload');
    await prompt.dismiss();
  }
  await cancelReload();
  await page.evaluate((value) => { location.hash = `project=${value}`; }, id);
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveValue('今天我们克汉口。');
  await expect(page.getByRole('button', { name: '保存文案', exact: true })).toBeEnabled();
  await page.getByRole('link', { name: '关闭项目详情' }).click();
  await expect(page.getByRole('region', { name: '文案工作台' })).toHaveCount(0);
  await cancelReload();
  await page.evaluate((value) => { location.hash = `project=${value}`; }, id);
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveValue('今天我们克汉口。');
  await expect(page.getByRole('button', { name: '保存文案', exact: true })).toBeEnabled();
});

test('重新载入成功后，迟到的初始读取失败不能覆盖成功状态', async ({ page }) => {
  const id = (await (await page.request.post('/api/projects', { data: { name: '迟到读取失败' } })).json() as { id: string }).id;
  let release!: () => void;
  let captured!: () => void;
  let delivered!: () => void;
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  const started = new Promise<void>((resolve) => { captured = resolve; });
  const finished = new Promise<void>((resolve) => { delivered = resolve; });
  await page.route(`**/api/projects/${id}/text`, async (route) => {
    captured(); await waiting;
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { message: '旧请求读取失败' } }) });
    delivered();
  }, { times: 1 });
  await page.goto(`/#project=${id}`);
  await started;
  await page.getByRole('button', { name: '重新载入服务器版本', exact: true }).click();
  await expect(page.getByRole('status', { name: '文案操作状态' })).toContainText('已载入服务器版本');
  release(); await finished;
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.getByRole('region', { name: '文案工作台' }).getByRole('alert')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '添加段落' })).toBeEnabled();
});

test('返回有缓存项目时读取延迟或失败仍展示本地稿，重载仅在明确接受后丢弃', async ({ page }) => {
  const id = await create(page);
  await fill(page);
  await save(page);
  await page.getByLabel('武汉话草稿', { exact: true }).fill('必须保护的未保存草稿');
  const next = await (await page.request.post('/api/projects', { data: { name: '缓存切换目标' } })).json() as { id: string };
  await page.evaluate((value) => { location.hash = `project=${value}`; }, next.id);
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveCount(0);
  let release!: () => void;
  let captured!: () => void;
  let delivered!: () => void;
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  const started = new Promise<void>((resolve) => { captured = resolve; });
  const finished = new Promise<void>((resolve) => { delivered = resolve; });
  await page.route(`**/api/projects/${id}/text`, async (route) => {
    captured(); await waiting;
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { message: '缓存项目首次读取失败' } }) });
    delivered();
  }, { times: 1 });
  await page.evaluate((value) => { location.hash = `project=${value}`; }, id);
  await started;
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveValue('必须保护的未保存草稿');
  await expect(page.getByRole('button', { name: '保存文案', exact: true })).toBeEnabled();
  page.once('dialog', async (dialog) => {
    expect(dialog.type()).toBe('confirm');
    expect(dialog.message()).toContain('丢弃本地未保存文案');
    await dialog.dismiss();
  });
  await page.getByRole('button', { name: '重新载入服务器版本', exact: true }).click();
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveValue('必须保护的未保存草稿');
  release(); await finished;
  await expect(page.getByRole('region', { name: '文案工作台' }).getByRole('alert')).toContainText('缓存项目首次读取失败');
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveValue('必须保护的未保存草稿');
  page.once('dialog', async (dialog) => { expect(dialog.type()).toBe('confirm'); await dialog.accept(); });
  await page.getByRole('button', { name: '重新载入服务器版本', exact: true }).click();
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveValue('今天我们克汉口。');
  await expect(page.getByRole('button', { name: '保存文案', exact: true })).toBeDisabled();
});
