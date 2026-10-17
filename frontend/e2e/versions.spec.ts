import { expect, test, type Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import type { TextDocument } from '../src/TextPanel';
import type { VersionCatalogue } from '../src/VersionPanel';

const panel = (page: Page) => page.getByRole('region', { name: '段落版本与局部返修', exact: true });
const paragraph = (page: Page, index: number) => panel(page).getByRole('region', { name: `第 ${index} 段版本`, exact: true });
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
async function read(page: Page, id: string) {
  return await (await page.request.get(`/api/projects/${id}/paragraph-versions?adapter_id=simulated-tone`)).json() as VersionCatalogue;
}
async function open(page: Page, id: string) {
  await page.goto(`/#project=${id}`);
  await expect(page.locator('article[data-task-id]')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '提交模拟短段任务', exact: true })).toBeEnabled();
  await expect(paragraph(page, 1).getByRole('button', { name: '选用此版本', exact: true })).toBeEnabled();
}
async function select(page: Page, index: number, stale = false) {
  await paragraph(page, index).getByRole('button', { name: stale ? '仍选用此过时版本' : '选用此版本', exact: true }).first().click();
  await expect(panel(page).getByRole('status', { name: '版本操作状态' })).toContainText(`已保存第 ${index} 段`);
  await expect(panel(page).getByRole('button', { name: '刷新版本', exact: true })).toBeEnabled();
}
async function saveAndConfirm(page: Page) {
  await page.getByRole('button', { name: '保存文案', exact: true }).click();
  await expect(page.getByRole('status', { name: '文案操作状态' })).toContainText('已保存');
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: '确认当前文案', exact: true }).click();
  await expect(page.getByRole('status', { name: '文案操作状态' })).toContainText('已确认');
}

test('真实段播放、选用持久化、仅改段过时及重生成不覆盖选用', async ({ page }, testInfo) => {
  const id = await seed(page); await open(page, id);
  const original = (await read(page, id)).paragraphs[0].versions[0].id;
  const audio = paragraph(page, 1).getByLabel('试听版本 1', { exact: true });
  await audio.evaluate(async (a) => { await (a as HTMLAudioElement).play(); });
  await expect.poll(() => audio.evaluate((a) => (a as HTMLAudioElement).currentTime)).toBeGreaterThan(0.01);
  await select(page, 1); await page.reload();
  await expect(paragraph(page, 1)).toContainText('已选用版本 1');
  await page.getByLabel('武汉话草稿', { exact: true }).first().fill('已改过的本段文案');
  await expect(panel(page)).toContainText('不包含本地修改');
  await expect(paragraph(page, 1).getByRole('button', { name: '按当前确认稿重生成本段' })).toBeDisabled();
  await saveAndConfirm(page);
  await expect(paragraph(page, 1)).toContainText('本段草稿已改变');
  await expect(paragraph(page, 2)).toContainText('匹配已保存稿');
  await select(page, 1, true);
  await expect(paragraph(page, 1)).toContainText('已过时');
  await paragraph(page, 1).getByRole('button', { name: '按当前确认稿重生成本段' }).click();
  await expect(paragraph(page, 1).locator('article[data-version-id]')).toHaveCount(2, { timeout: 15000 });
  const current = await read(page, id);
  expect(current.paragraphs[0].selection?.version_id).toBe(original);
  expect(current.paragraphs[0].versions[0].comparison).toBe('matching');
  expect(current.paragraphs[0].versions[1].comparison).toBe('stale');
  expect(current.paragraphs[1].versions).toHaveLength(1);
  await select(page, 1); await page.reload();
  await expect(paragraph(page, 1)).toContainText('已选用版本 2');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await paragraph(page, 1).screenshot({ path: testInfo.outputPath('versions.png') });
});

test('多窗口选用冲突与响应丢失均要求刷新核对，旧显示不冒充成功', async ({ page, context }) => {
  const id = await seed(page); await open(page, id);
  const other = await context.newPage(); await open(other, id);
  await select(page, 1);
  await paragraph(other, 1).getByRole('button', { name: '选用此版本', exact: true }).click();
  await expect(panel(other).getByRole('alert')).toContainText('其他窗口');
  await expect(paragraph(other, 1)).toContainText('尚未选用版本');
  await panel(other).getByRole('button', { name: '刷新版本', exact: true }).click();
  await expect(paragraph(other, 1)).toContainText('已选用版本 1');
  await other.route(`**/api/projects/${id}/paragraphs/*/selection`, async (route) => { await route.fetch(); await route.abort(); });
  await paragraph(other, 2).getByRole('button', { name: '选用此版本', exact: true }).click();
  await expect(panel(other).getByRole('alert')).toContainText('选用结果需要核对');
  await expect(paragraph(other, 2)).toContainText('尚未选用版本');
  await panel(other).getByRole('button', { name: '刷新版本', exact: true }).click();
  await expect(paragraph(other, 2)).toContainText('已选用版本 1');
  await other.close();
});

test('前后段按选用顺序实际播放，缺项与文件播放失败明确停止', async ({ page }) => {
  const id = await seed(page); await open(page, id);
  await paragraph(page, 2).getByRole('button', { name: '前后段试听', exact: true }).click();
  const neighbors = panel(page).getByRole('region', { name: '前后段试听', exact: true });
  await expect(neighbors).toContainText('邻段缺少可用选用');
  await expect(neighbors.getByRole('button', { name: '开始连续试听' })).toBeDisabled();
  await select(page, 1); await select(page, 3);
  await paragraph(page, 2).getByRole('button', { name: '前后段试听', exact: true }).click();
  await page.evaluate(() => {
    const log: string[] = []; (window as unknown as { played: string[] }).played = log;
    document.addEventListener('playing', (event) => { const a = event.target as HTMLAudioElement;
      if (a.getAttribute('aria-label') === '邻段连续播放器') log.push(a.currentSrc); }, true);
  });
  await neighbors.getByRole('button', { name: '开始连续试听' }).click();
  await expect(neighbors.getByRole('status', { name: '连续试听状态' })).toContainText('连续试听完成');
  const catalog = await read(page, id);
  const played = await page.evaluate(() => (window as unknown as { played: string[] }).played);
  expect(played.map((url) => url.split('/').at(-2))).toEqual(catalog.paragraphs.map((p) => p.versions[0].id));
  await page.route('**/paragraph-versions/*/audio', (route) => route.abort());
  await neighbors.getByRole('button', { name: '开始连续试听' }).click();
  await expect(neighbors.getByRole('status', { name: '连续试听状态' })).toContainText('播放失败');
});

test('版本读取失败可恢复，项目切换隔离迟到列表，删除后保留历史', async ({ page }) => {
  const id = await seed(page); const next = await seed(page);
  let releaseTasks!: () => void;
  const taskGate = new Promise<void>((resolve) => { releaseTasks = resolve; });
  await page.route(`**/api/projects/${id}/tasks`, async (route) => {
    const response = await route.fetch(); await taskGate; await route.fulfill({ response });
  }, { times: 1 });
  await page.goto(`/#project=${id}`);
  const choose = paragraph(page, 1).getByRole('button', { name: '选用此版本', exact: true });
  await expect(choose).toBeEnabled();
  await expect(page.locator('article[data-task-id]')).toHaveCount(0);
  const url = `**/api/projects/${id}/paragraph-versions?*`;
  await page.route(url, (route) => route.abort());
  releaseTasks();
  await expect(page.locator('article[data-task-id]')).toHaveCount(1);
  // Initial task data changes the refresh stamp; the older catalogue cannot remain actionable.
  await expect(panel(page).getByRole('alert')).toContainText('版本读取失败');
  await expect(choose).toHaveCount(0);
  await page.unroute(url);
  await panel(page).getByRole('button', { name: '刷新版本', exact: true }).click();
  await expect(page.getByRole('button', { name: '提交模拟短段任务', exact: true })).toBeEnabled();
  await expect(choose).toBeEnabled();
  // With all initial inputs loaded, failure of an explicit refresh must retain and disable the candidate.
  await page.route(url, (route) => route.abort());
  await panel(page).getByRole('button', { name: '刷新版本', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toContainText('版本读取失败');
  await expect(paragraph(page, 1).getByRole('button', { name: '选用此版本', exact: true })).toBeDisabled();
  await page.unroute(url);
  let release!: () => void; let captured!: () => void; let delivered!: () => void;
  const waiting = new Promise<void>((r) => { release = r; });
  const started = new Promise<void>((r) => { captured = r; });
  const ended = new Promise<void>((r) => { delivered = r; });
  await page.route(url, async (route) => { const response = await route.fetch(); captured(); await waiting; await route.fulfill({ response }); delivered(); }, { times: 1 });
  await panel(page).getByRole('button', { name: '刷新版本', exact: true }).click(); await started;
  await page.evaluate((id) => { location.hash = `project=${id}`; }, next);
  const nextVersion = (await read(page, next)).paragraphs[0].versions[0].id;
  await expect(paragraph(page, 1).locator('article')).toHaveAttribute('data-version-id', nextVersion);
  release(); await ended;
  await expect(paragraph(page, 1).locator('article')).toHaveAttribute('data-version-id', nextVersion);
  await page.getByRole('article', { name: '第 1 段', exact: true }).getByRole('button', { name: '删除段落' }).click();
  await page.getByRole('button', { name: '保存文案', exact: true }).click();
  await expect(panel(page).getByText('已删除段落的历史（1 个版本）')).toBeVisible();
  await panel(page).getByText('已删除段落的历史（1 个版本）').click();
  await expect(panel(page)).toContainText('已从当前稿删除');
});

test('其他窗口改稿使旧比较失效，刷新后明确过时；重排保留选择并按新顺序试听', async ({ page }) => {
  const id = await seed(page); await open(page, id);
  for (const index of [1, 2, 3]) await select(page, index);
  const before = await read(page, id);
  const doc = await (await page.request.get(`/api/projects/${id}/text`)).json() as TextDocument;
  doc.paragraphs[0].draft_text = '另一个窗口已保存的文字';
  const response = await page.request.put(`/api/projects/${id}/text`, { data: {
    expected_revision: doc.revision, source_language: doc.source_language, target_language: doc.target_language,
    register: doc.register, terms: doc.terms, paragraphs: doc.paragraphs,
  } });
  expect(response.status()).toBe(200);
  await paragraph(page, 1).getByRole('button', { name: '选用此版本', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toContainText('输入已改变');
  await panel(page).getByRole('button', { name: '刷新版本', exact: true }).click();
  await expect(paragraph(page, 1)).toContainText('已过时');
  await page.getByRole('button', { name: '重新载入服务器版本', exact: true }).click();
  await expect(page.getByLabel('武汉话草稿', { exact: true }).first()).toHaveValue('另一个窗口已保存的文字');
  await page.getByRole('article', { name: '第 3 段', exact: true }).getByRole('button', { name: '上移' }).click();
  await page.getByRole('button', { name: '保存文案', exact: true }).click();
  await expect(paragraph(page, 2)).toHaveAttribute('data-version-paragraph', before.paragraphs[2].id);
  await expect(paragraph(page, 3)).toHaveAttribute('data-version-paragraph', before.paragraphs[1].id);
  await paragraph(page, 2).getByRole('button', { name: '前后段试听' }).click();
  const neighbors = panel(page).getByRole('region', { name: '前后段试听', exact: true });
  await expect(neighbors).toContainText('已过时');
  await page.evaluate(() => { const log: string[] = []; (window as unknown as { played: string[] }).played = log;
    document.addEventListener('playing', (e) => { const a = e.target as HTMLAudioElement;
      if (a.getAttribute('aria-label') === '邻段连续播放器') log.push(a.currentSrc); }, true); });
  await neighbors.getByRole('button', { name: '开始连续试听' }).click();
  await expect(neighbors.getByRole('status', { name: '连续试听状态' })).toContainText('连续试听完成');
  const played = await page.evaluate(() => (window as unknown as { played: string[] }).played);
  expect(played.map((url) => url.split('/').at(-2))).toEqual([0, 2, 1].map((i) => before.paragraphs[i].versions[0].id));
});

test('连续试听中途停止后从首段起点重开，首段网络失败解除后可完整恢复', async ({ page }) => {
  const id = await seed(page); await open(page, id);
  for (const index of [1, 2, 3]) await select(page, index);
  await paragraph(page, 2).getByRole('button', { name: '前后段试听' }).click();
  const neighbors = panel(page).getByRole('region', { name: '前后段试听', exact: true });
  await page.evaluate(() => { const starts: number[] = []; (window as unknown as { starts: number[] }).starts = starts;
    document.addEventListener('playing', (e) => { const audio = e.target as HTMLAudioElement;
      if (audio.getAttribute('aria-label') === '邻段连续播放器') { starts.push(audio.currentTime); if (starts.length === 1) audio.playbackRate = 0.1; } }, true); });
  await neighbors.getByRole('button', { name: '开始连续试听' }).click();
  const audio = neighbors.getByLabel('邻段连续播放器', { exact: true });
  await expect.poll(() => audio.evaluate((a) => (a as HTMLAudioElement).currentTime)).toBeGreaterThan(0.04);
  await neighbors.getByRole('button', { name: '停止试听' }).click();
  await expect(neighbors.getByRole('status', { name: '连续试听状态' })).toContainText('已停止');
  const stopped = await audio.evaluate((a) => (a as HTMLAudioElement).currentTime);
  expect(stopped).toBeGreaterThan(0.04);
  await neighbors.getByRole('button', { name: '开始连续试听' }).click();
  await expect(neighbors.getByRole('status', { name: '连续试听状态' })).toContainText('连续试听完成');
  const starts = await page.evaluate(() => (window as unknown as { starts: number[] }).starts);
  expect(starts).toHaveLength(4); expect(starts[1]).toBeLessThan(0.02);
  const route = '**/paragraph-versions/*/audio';
  await page.route(route, (r) => r.abort());
  await neighbors.getByRole('button', { name: '开始连续试听' }).click();
  await expect(neighbors.getByRole('status', { name: '连续试听状态' })).toContainText('播放失败');
  await page.unroute(route);
  await neighbors.getByRole('button', { name: '开始连续试听' }).click();
  await expect(neighbors.getByRole('status', { name: '连续试听状态' })).toContainText('连续试听完成');
  expect(await page.evaluate(() => (window as unknown as { starts: number[] }).starts.length)).toBe(7);
});
