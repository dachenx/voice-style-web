import { expect, test, type Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import type { TextDocument } from '../src/TextPanel';
import type { VersionCatalogue } from '../src/VersionPanel';
function wave() {
  const b = Buffer.alloc(44 + 48000); b.write('RIFF'); b.writeUInt32LE(b.length - 8, 4); b.write('WAVEfmt ', 8);
  b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22); b.writeUInt32LE(24000, 24);
  b.writeUInt32LE(48000, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write('data', 36); b.writeUInt32LE(48000, 40);
  return b;
}
async function seed(page: Page) {
  const { id } = await (await page.request.post('/api/projects', { data: { name: '段版本测试' } })).json() as { id: string };
  const base = `/api/projects/${id}`;
  await page.request.put(`${base}/text`, { data: { expected_revision: 0, source_language: 'zh-CN', target_language: 'wuhan', register: '', terms: '',
    paragraphs: Array.from({ length: 3 }, (_, n) => ({ id: randomUUID(), original_text: `人工原稿${n}`, draft_text: `人工草稿${n}`, pronunciation_note: '' })) } });
  const doc = await (await page.request.post(`${base}/text/confirm`, { data: { expected_revision: 1, acknowledged: true } })).json() as TextDocument;
  const ref = await (await page.request.post(`${base}/references?filename=fixture.wav`, { headers: { 'Content-Type': 'application/octet-stream' }, data: wave() })).json() as { id: string };
  const clip = await (await page.request.post(`${base}/references/${ref.id}/clips`, { data: { start_ms: 0, end_ms: 900 } })).json() as { id: string };
  const response = await page.request.post(`${base}/tasks`, { data: { request_id: randomUUID(), expected_revision: doc.revision,
    expected_confirmation_id: doc.latest_confirmation!.id, expected_clip_id: clip.id, adapter_id: 'simulated-tone', mode: 'full', paragraph_id: null, parameters: {} } });
  expect(response.status()).toBe(201);
  const task = await response.json() as { id: string };
  await expect.poll(async () => (await (await page.request.get(`${base}/tasks/${task.id}`)).json() as { state: string }).state).toBe('succeeded');
  return id;
}

const panel = (page: Page) => page.getByRole('region', { name: '拼接与导出', exact: true });
async function ready(page: Page) {
  const id = await seed(page);
  const base = `/api/projects/${id}`;
  for (let index = 0; index < 3; index++) {
    const c = await (await page.request.get(`${base}/paragraph-versions?adapter_id=simulated-tone`)).json() as VersionCatalogue;
    const p = c.paragraphs[index];
    expect((await page.request.put(`${base}/paragraphs/${p.id}/selection`, { data: {
      version_id: p.versions[0].id, expected_revision: 0, adapter_id: 'simulated-tone', comparison_token: c.comparison_token,
    } })).ok()).toBeTruthy();
  }
  await page.goto(`/#project=${id}`);
  await expect(page.locator('article[data-task-id]')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '提交模拟短段任务', exact: true })).toBeEnabled();
  await expect(panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true })).toBeEnabled();
  return id;
}

test('整篇与单段真实下载、停顿来源、刷新恢复', async ({ page }, testInfo) => {
  const id = await ready(page);
  await panel(page).getByRole('spinbutton').fill('500');
  await panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true }).click();
  await expect(panel(page).getByRole('status')).toContainText('导出已保存');
  await expect(panel(page).getByText('含模拟测试音，不朗读文案', { exact: false })).toBeVisible();
  const link = panel(page).getByRole('link', { name: '下载 WAV', exact: true });
  const download = page.waitForEvent('download'); await link.click();
  const file = await download; expect(file.suggestedFilename()).toMatch(/^simulated-.*\.wav$/);
  expect(await file.failure()).toBeNull();
  const manifests = await (await page.request.get(`/api/projects/${id}/exports`)).json();
  expect(manifests[0].pause_ms).toBe(500); expect(manifests[0].segments).toHaveLength(3);
  await page.reload(); await expect(panel(page).getByRole('link', { name: '下载 WAV', exact: true })).toHaveCount(1);
  await panel(page).getByRole('button', { name: '导出第 2 段 WAV', exact: true }).click();
  await expect(panel(page).getByRole('link', { name: '下载 WAV', exact: true })).toHaveCount(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel(page).screenshot({ path: testInfo.outputPath('exports.png') });
});

test('响应丢失后同请求恢复不重复，跨项目隔离', async ({ page }) => {
  const id = await ready(page); let once = true;
  await page.route(`**/api/projects/${id}/exports`, async route => {
    if (route.request().method() === 'POST' && once) { once = false; await route.fetch(); await route.abort(); }
    else await route.continue();
  });
  await panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toContainText('需要核对');
  await panel(page).getByRole('button', { name: '重试同一导出请求' }).click();
  await expect(panel(page).getByRole('status')).toContainText('导出已保存');
  expect(await (await page.request.get(`/api/projects/${id}/exports`)).json()).toHaveLength(1);
  const other = await ready(page);
  expect(other).not.toBe(id);
  await expect(panel(page).getByRole('link', { name: '下载 WAV', exact: true })).toHaveCount(0);
});

test('未保存与未确认阻止，过时确认后导出，勾选在选用改变后失效', async ({ page }) => {
  await ready(page);
  await page.getByLabel('武汉话草稿', { exact: true }).first().fill('更新草稿');
  await expect(panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '保存文案', exact: true }).click();
  await expect(page.getByRole('status', { name: '文案操作状态' })).toContainText('已保存');
  await expect(panel(page)).toContainText('请先确认已保存文案');
  await page.getByRole('checkbox', { name: /我已逐段/ }).check();
  await page.getByRole('button', { name: '确认当前文案', exact: true }).click();
  await expect(page.getByRole('status', { name: '文案操作状态' })).toContainText('已确认');
  await expect(panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true })).toBeDisabled();
  await panel(page).getByRole('checkbox').check();
  await expect(panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '仍选用此过时版本', exact: true }).click();
  await expect(panel(page).getByRole('checkbox')).not.toBeChecked();
  await panel(page).getByRole('checkbox').check();
  await panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true }).click();
  await expect(panel(page)).toContainText('导出时包含过时或无法核定版本');
});

test('迟到的旧历史不覆盖已成功的新导出', async ({ page }) => {
  const id = await ready(page);
  let release!: () => void;
  const barrier = new Promise<void>(resolve => { release = resolve; });
  let captured!: () => void;
  const capturedResponse = new Promise<void>(resolve => { captured = resolve; });
  let released!: () => void;
  const releasedResponse = new Promise<void>(resolve => { released = resolve; });
  let held = false;
  await page.route(`**/api/projects/${id}/exports`, async route => {
    if (route.request().method() === 'GET' && !held) {
      held = true; const response = await route.fetch(); captured(); await barrier;
      await route.fulfill({ response }); released();
    } else await route.continue();
  });
  await panel(page).getByRole('button', { name: '刷新导出历史' }).click();
  await capturedResponse;
  const fresh = page.waitForResponse(r => r.url().endsWith(`/projects/${id}/exports`) && r.request().method() === 'GET' && r.ok());
  await panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true }).click();
  await expect(panel(page).getByRole('link', { name: '下载 WAV', exact: true })).toHaveCount(1);
  // Completion refresh may abort the obsolete GET; wait for the held route to finish.
  const authoritative = await (await fresh).json() as { id: string }[];
  expect(authoritative).toHaveLength(1);
  await expect(panel(page).getByRole('link', { name: '下载 WAV', exact: true })).toHaveAttribute('href', `/api/projects/${id}/exports/${authoritative[0].id}/audio`);
  release(); await releasedResponse;
  await expect(panel(page).getByRole('link', { name: '下载 WAV', exact: true })).toHaveCount(1);
});

test('迟到历史不能清掉提交结果未知提示', async ({ page }) => {
  const id = await ready(page);
  let release!: () => void; const barrier = new Promise<void>(r => { release = r; });
  let captured!: () => void; const capturedResponse = new Promise<void>(r => { captured = r; });
  let held = false; let fail = true;
  await page.route(`**/api/projects/${id}/exports`, async route => {
    if (route.request().method() === 'GET' && !held) {
      held = true; const response = await route.fetch(); captured(); await barrier; await route.fulfill({ response });
    } else if (route.request().method() === 'POST' && fail) {
      fail = false; await route.fetch(); await route.abort();
    } else await route.continue();
  });
  await panel(page).getByRole('button', { name: '刷新导出历史' }).click(); await capturedResponse;
  await panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toContainText('结果需要核对');
  const response = page.waitForResponse(r => r.url().endsWith(`/projects/${id}/exports`) && r.request().method() === 'GET');
  release(); await response;
  await expect(panel(page).getByRole('alert')).toContainText('结果需要核对');
  await panel(page).getByRole('button', { name: '重试同一导出请求' }).click();
  await expect(panel(page).getByRole('status')).toContainText('导出已保存');
  expect(await (await page.request.get(`/api/projects/${id}/exports`)).json()).toHaveLength(1);
});
