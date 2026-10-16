import { expect, test, type Locator, type Page } from '@playwright/test';
import { randomUUID, createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import type { Project } from '../src/api';
import type { TextDocument } from '../src/TextPanel';

type Clip = { id: string; reference_id: string; start_ms: number; end_ms: number };
type Reference = { id: string; filename: string };
type Task = {
  id: string; project_id: string; mode: 'preview' | 'full'; state: string;
  state_version: number; is_simulated: boolean; retry_of: string | null;
  completed_paragraphs: number; total_paragraphs: number;
  error: unknown;
  result: null | { filename: string; byte_size: number; sha256: string; duration_ms: number };
  snapshot: {
    confirmation: NonNullable<TextDocument['latest_confirmation']>;
    reference: Reference & { clip: Clip; clip_sha256: string };
    parameters: Record<string, unknown>; paragraph_ids: string[]; is_simulated: boolean;
  };
};
type TaskList = { tasks: Omit<Task, 'snapshot'>[]; worker: { state: string } };
type Seed = { id: string; document: TextDocument; reference: Reference | null; clip: Clip | null };
const panel = (page: Page) => page.getByRole('region', { name: '模拟生成与任务' });
const card = (page: Page, id: string) => panel(page).locator(`article[data-task-id="${id}"]`);
const submitButton = (page: Page, mode: 'preview' | 'full') => panel(page).getByRole('button', {
  name: mode === 'preview' ? '提交模拟短段任务' : '提交模拟全文任务', exact: true,
});

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

// All source audio is an in-memory synthetic tone, never a person's recording.
function tone(): Buffer {
  const frames = 24000;
  const data = Buffer.alloc(44 + frames * 2);
  data.write('RIFF', 0); data.writeUInt32LE(data.length - 8, 4); data.write('WAVEfmt ', 8);
  data.writeUInt32LE(16, 16); data.writeUInt16LE(1, 20); data.writeUInt16LE(1, 22);
  data.writeUInt32LE(24000, 24); data.writeUInt32LE(48000, 28);
  data.writeUInt16LE(2, 32); data.writeUInt16LE(16, 34);
  data.write('data', 36); data.writeUInt32LE(frames * 2, 40);
  for (let frame = 0; frame < frames; frame += 1) {
    data.writeInt16LE(Math.round(4000 * Math.sin(2 * Math.PI * 400 * frame / 24000)), 44 + frame * 2);
  }
  return data;
}

async function saveClip(page: Page, id: string, referenceId: string, start = 0, end = 800) {
  const response = await page.request.post(`/api/projects/${id}/references/${referenceId}/clips`, {
    data: { start_ms: start, end_ms: end },
  });
  expect(response.status()).toBe(201);
  return await response.json() as Clip;
}

async function addReference(page: Page, id: string) {
  const response = await page.request.post(`/api/projects/${id}/references?filename=synthetic-task-reference.wav`, {
    headers: { 'Content-Type': 'application/octet-stream' }, data: tone(),
  });
  expect(response.status()).toBe(201);
  const reference = await response.json() as Reference;
  return { reference, clip: await saveClip(page, id, reference.id) };
}

async function seed(page: Page, name: string, options: { reference?: boolean; confirmed?: boolean; paragraphs?: number } = {}): Promise<Seed> {
  const created = await page.request.post('/api/projects', { data: { name } });
  expect(created.status()).toBe(201);
  const { id } = await created.json() as { id: string };
  const saved = await page.request.put(`/api/projects/${id}/text`, { data: {
    expected_revision: 0, source_language: 'zh-CN', target_language: 'wuhan', register: '自然口语', terms: '',
    paragraphs: Array.from({ length: options.paragraphs ?? 2 }, (_, index) => ({
      id: randomUUID(), original_text: `人工原稿第 ${index + 1} 段，金额 123 元。`,
      draft_text: `人工草稿第 ${index + 1} 段，金额 123 元。`, pronunciation_note: '仅验证流程，不验证发音',
    })),
  } });
  expect(saved.ok()).toBe(true);
  let document = await saved.json() as TextDocument;
  if (options.confirmed !== false) {
    const confirmed = await page.request.post(`/api/projects/${id}/text/confirm`, {
      data: { expected_revision: document.revision, acknowledged: true },
    });
    expect(confirmed.ok()).toBe(true);
    document = await confirmed.json() as TextDocument;
  }
  const audio = options.reference === false ? { reference: null, clip: null } : await addReference(page, id);
  return { id, document, ...audio };
}

async function open(page: Page, value: Seed) {
  await page.goto(`/#project=${value.id}`);
  await expect(panel(page)).toBeVisible();
  await expect(page.getByLabel('武汉话草稿', { exact: true }).first()).toHaveValue(value.document.paragraphs[0].draft_text);
}

async function taskList(page: Page, id: string) {
  const response = await page.request.get(`/api/projects/${id}/tasks`);
  expect(response.ok()).toBe(true);
  return await response.json() as TaskList;
}

async function taskDetail(page: Page, projectId: string, taskId: string) {
  const response = await page.request.get(`/api/projects/${projectId}/tasks/${taskId}`);
  expect(response.ok()).toBe(true);
  return await response.json() as Task;
}

async function submit(page: Page, id: string, mode: 'preview' | 'full') {
  await expect(submitButton(page, mode)).toBeEnabled();
  const response = page.waitForResponse((value) => value.request().method() === 'POST'
    && new URL(value.url()).pathname === `/api/projects/${id}/tasks`);
  await submitButton(page, mode).click();
  const received = await response;
  expect(received.status()).toBe(201);
  const value = await received.json() as Task;
  await expect(card(page, value.id)).toBeVisible();
  return value;
}

async function succeeded(page: Page, projectId: string, taskId: string) {
  await expect.poll(async () => (await taskDetail(page, projectId, taskId)).state, { timeout: 20000 }).toBe('succeeded');
  const task = await taskDetail(page, projectId, taskId);
  expect(task.is_simulated).toBe(true);
  expect(task.result).not.toBeNull();
  expect(task.completed_paragraphs).toBe(task.total_paragraphs);
  await expect(card(page, taskId).getByLabel('模拟任务结果', { exact: true })).toBeVisible({ timeout: 10000 });
  return task;
}

function validateWave(bytes: Buffer) {
  expect(bytes.toString('ascii', 0, 4)).toBe('RIFF');
  expect(bytes.toString('ascii', 8, 12)).toBe('WAVE');
  expect(bytes.readUInt32LE(4) + 8).toBe(bytes.length);
  let byteRate = 0;
  let samples: Buffer | null = null;
  for (let offset = 12; offset + 8 <= bytes.length;) {
    const name = bytes.toString('ascii', offset, offset + 4);
    const size = bytes.readUInt32LE(offset + 4);
    expect(offset + 8 + size).toBeLessThanOrEqual(bytes.length);
    if (name === 'fmt ') {
      expect(size).toBeGreaterThanOrEqual(16);
      expect(bytes.readUInt16LE(offset + 8)).toBe(1);
      expect(bytes.readUInt16LE(offset + 10)).toBeGreaterThan(0);
      byteRate = bytes.readUInt32LE(offset + 16);
    }
    if (name === 'data') samples = bytes.subarray(offset + 8, offset + 8 + size);
    offset += 8 + size + size % 2;
  }
  expect(byteRate).toBeGreaterThan(0);
  expect(samples).not.toBeNull();
  expect(samples!.length).toBeGreaterThan(0);
  expect(samples!.some((value) => value !== 0)).toBe(true);
  return samples!.length / byteRate;
}

async function play(player: Locator, duration: number) {
  await expect.poll(() => player.evaluate((element) => (element as HTMLAudioElement).readyState)).toBeGreaterThanOrEqual(2);
  expect(await player.evaluate((element) => (element as HTMLAudioElement).duration)).toBeCloseTo(duration, 2);
  await player.evaluate(async (element) => { await (element as HTMLAudioElement).play(); });
  await expect.poll(() => player.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThan(0.03);
  await player.evaluate((element) => { (element as HTMLAudioElement).pause(); });
}

async function rendered(page: Page) {
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
}

test('无参考、未确认和未保存稿均禁提交，完成核对后才可模拟生成', async ({ page }) => {
  const value = await seed(page, '模拟生成输入门槛', { reference: false, confirmed: false });
  await open(page, value);
  await expect(submitButton(page, 'preview')).toBeDisabled();
  await expect(submitButton(page, 'full')).toBeDisabled();
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: '确认当前文案', exact: true }).click();
  await expect(page.getByRole('status', { name: '文案操作状态' })).toContainText('文案已确认');
  await expect(submitButton(page, 'full')).toBeDisabled();
  await addReference(page, value.id);
  await panel(page).getByRole('button', { name: '刷新参考与组件', exact: true }).click();
  await expect(submitButton(page, 'full')).toBeEnabled();
  await page.getByLabel('武汉话草稿', { exact: true }).first().fill('尚未保存的任务输入');
  await expect(submitButton(page, 'preview')).toBeDisabled();
  await expect(submitButton(page, 'full')).toBeDisabled();
  await page.getByRole('button', { name: '保存文案', exact: true }).click();
  await expect(page.getByRole('status', { name: '文案操作状态' })).toContainText('文案已保存');
  await expect(submitButton(page, 'full')).toBeDisabled();
  expect((await taskList(page, value.id)).tasks).toHaveLength(0);
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: '确认当前文案', exact: true }).click();
  await expect(submitButton(page, 'full')).toBeEnabled();
  await expect(submitButton(page, 'preview')).toBeEnabled();
});

test('无需先短段即可提交全文，真实模拟结果可播放并下载相同有效 WAV', async ({ page }) => {
  const value = await seed(page, '模拟结果播放下载');
  const newerResponse = await page.request.post('/api/projects', { data: { name: '任务提交前新建的对照项目' } });
  expect(newerResponse.status()).toBe(201);
  const newer = await newerResponse.json() as Project;
  const before = await (await page.request.get(`/api/projects/${value.id}`)).json() as Project;
  await open(page, value);
  const projectList = page.getByRole('region', { name: '项目列表' });
  await expect(projectList.getByRole('link').first()).toHaveAttribute('href', `#project=${newer.id}`);
  const full = await submit(page, value.id, 'full');
  const updated = await (await page.request.get(`/api/projects/${value.id}`)).json() as Project;
  expect(updated.updated_at).not.toBe(before.updated_at);
  await expect(projectList.getByRole('link').first()).toHaveAttribute('href', `#project=${value.id}`);
  await expect(projectList.locator(`a[href="#project=${value.id}"] time`)).toHaveAttribute('datetime', updated.updated_at);
  await expect(page.getByRole('region', { name: '项目详情' }).getByText('最近更新', { exact: true }).locator('..').locator('time'))
    .toHaveAttribute('datetime', updated.updated_at);
  expect((await taskList(page, value.id)).tasks).toHaveLength(1);
  const result = await succeeded(page, value.id, full.id);
  expect(result.mode).toBe('full');
  expect(result.snapshot.paragraph_ids).toEqual(value.document.paragraphs.map((paragraph) => paragraph.id));
  expect(result.snapshot.is_simulated).toBe(true);
  expect(result.snapshot.confirmation).toEqual(value.document.latest_confirmation);
  await expect(card(page, full.id)).toContainText('模拟');
  const response = await page.request.get(`/api/projects/${value.id}/tasks/${full.id}/audio`);
  expect(response.status()).toBe(200);
  expect(response.headers()['content-type']).toContain('audio/wav');
  const bytes = await response.body();
  const duration = validateWave(bytes);
  expect(bytes.length).toBe(result.result!.byte_size);
  expect(createHash('sha256').update(bytes).digest('hex')).toBe(result.result!.sha256);
  expect(duration * 1000).toBeCloseTo(result.result!.duration_ms, 0);
  await play(card(page, full.id).getByLabel('模拟任务结果', { exact: true }), duration);
  const downloadEvent = page.waitForEvent('download');
  await card(page, full.id).getByRole('link', { name: '下载模拟音频', exact: true }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe(result.result!.filename);
  expect(download.suggestedFilename()).toMatch(/^simulated-.*\.wav$/);
  const downloaded = await download.path();
  expect(downloaded).not.toBeNull();
  expect(await readFile(downloaded!)).toEqual(bytes);
  await panel(page).getByLabel('短段测试段落', { exact: true }).selectOption(value.document.paragraphs[1].id);
  const preview = await submit(page, value.id, 'preview');
  const previewResult = await succeeded(page, value.id, preview.id);
  expect(previewResult.mode).toBe('preview');
  expect(previewResult.snapshot.paragraph_ids).toEqual([value.document.paragraphs[1].id]);
  expect(previewResult.total_paragraphs).toBe(1);
  await page.reload();
  await expect(card(page, full.id).getByLabel('模拟任务结果', { exact: true })).toBeVisible();
  await expect(card(page, preview.id).getByLabel('模拟任务结果', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel(page).screenshot({ path: test.info().outputPath('模拟任务面板.png') });
});

test('入队后的文案和参考快照不会随新编辑或新选择改变', async ({ page }) => {
  const value = await seed(page, '任务不可变输入');
  await open(page, value);
  const submitted = await submit(page, value.id, 'full');
  const original = await taskDetail(page, value.id, submitted.id);
  expect(original.snapshot.confirmation).toEqual(value.document.latest_confirmation);
  expect(original.snapshot.reference.clip.id).toBe(value.clip!.id);
  await page.getByLabel('武汉话草稿', { exact: true }).first().fill('提交之后保存的新文案，金额 456 元。');
  await page.getByRole('button', { name: '保存文案', exact: true }).click();
  await expect(page.getByRole('status', { name: '文案操作状态' })).toContainText('文案已保存');
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: '确认当前文案', exact: true }).click();
  await expect(page.getByRole('status', { name: '文案操作状态' })).toContainText('文案已确认');
  const changedClip = await saveClip(page, value.id, value.reference!.id, 200, 900);
  expect(changedClip.id).not.toBe(value.clip!.id);
  await panel(page).getByRole('button', { name: '刷新参考与组件', exact: true }).click();
  const completed = await succeeded(page, value.id, submitted.id);
  expect(completed.snapshot).toEqual(original.snapshot);
  await card(page, submitted.id).getByRole('button', { name: '查看任务快照', exact: true }).click();
  const snapshot = panel(page).getByRole('region', { name: '任务输入快照' });
  await expect(snapshot).toContainText(value.document.paragraphs[0].draft_text);
  await expect(snapshot).toContainText(value.clip!.id);
  await expect(snapshot).not.toContainText('提交之后保存的新文案');
  await expect(snapshot).not.toContainText(changedClip.id);
});

test('服务器已入队但响应丢失时核对重放原请求，不重复建立任务', async ({ page }) => {
  const value = await seed(page, '任务请求丢失与重放');
  await open(page, value);
  const captured = deferred();
  let acceptedId = '';
  const bodies: unknown[] = [];
  await page.route(`**/api/projects/${value.id}/tasks`, async (route) => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    bodies.push(route.request().postDataJSON());
    if (bodies.length === 1) {
      const response = await route.fetch();
      expect(response.status()).toBe(201);
      acceptedId = (await response.json() as Task).id;
      await route.abort(); captured.resolve();
    } else await route.continue();
  });
  await expect(submitButton(page, 'full')).toBeEnabled();
  await submitButton(page, 'full').click();
  await captured.promise;
  await expect(panel(page).getByRole('alert')).toBeVisible();
  await page.reload();
  const retry = panel(page).getByRole('button', { name: '核对并重试上次提交', exact: true });
  await expect(retry).toBeEnabled();
  const replayResponse = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/projects/${value.id}/tasks`);
  await retry.click();
  const replay = await replayResponse;
  expect(replay.status()).toBe(200);
  const replayed = await replay.json() as Task;
  expect(bodies).toHaveLength(2);
  expect(bodies[1]).toEqual(bodies[0]);
  expect(replayed.id).toBe(acceptedId);
  expect((await taskList(page, value.id)).tasks.map((task) => task.id)).toEqual([replayed.id]);
  await succeeded(page, value.id, replayed.id);
});

test('任务读取失败可主动恢复，迟到的真实旧列表不能删除新任务', async ({ page }) => {
  const value = await seed(page, '任务列表失败与迟到');
  const url = `**/api/projects/${value.id}/tasks`;
  await page.route(url, async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 503, contentType: 'application/json',
        body: JSON.stringify({ error: { message: '任务列表暂不可读取' } }) });
    } else await route.continue();
  });
  await open(page, value);
  await expect(panel(page).getByRole('alert')).toContainText('任务列表暂不可读取');
  await page.unroute(url);
  await panel(page).getByRole('button', { name: '刷新任务', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toHaveCount(0);
  const captured = deferred();
  const released = deferred();
  const delivered = deferred();
  await page.route(url, async (route) => {
    const response = await route.fetch();
    captured.resolve(); await released.promise;
    await route.fulfill({ response }); delivered.resolve();
  }, { times: 1 });
  await panel(page).getByRole('button', { name: '刷新任务', exact: true }).click();
  await captured.promise;
  const created = await submit(page, value.id, 'full');
  released.resolve(); await delivered.promise;
  await rendered(page);
  await expect(card(page, created.id)).toBeVisible();
  await succeeded(page, value.id, created.id);
});

test('其他窗口切换当前参考后拒绝旧选择入队，主动核对后使用新参考', async ({ page }) => {
  const value = await seed(page, '外部参考选择冲突');
  await open(page, value);
  await expect(submitButton(page, 'full')).toBeEnabled();
  const replacement = await saveClip(page, value.id, value.reference!.id, 100, 900);
  const rejected = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/projects/${value.id}/tasks`);
  await submitButton(page, 'full').click();
  expect((await rejected).status()).toBe(409);
  await expect(panel(page).getByRole('alert')).toBeVisible();
  expect((await taskList(page, value.id)).tasks).toHaveLength(0);
  await panel(page).getByRole('button', { name: '刷新参考与组件', exact: true }).click();
  const created = await submit(page, value.id, 'full');
  expect((await taskDetail(page, value.id, created.id)).snapshot.reference.clip.id).toBe(replacement.id);
  await succeeded(page, value.id, created.id);
});

test('工作进程已完成并刷新后，迟到的真实入队响应不能把任务回退为排队', async ({ page }) => {
  const value = await seed(page, '真实任务状态防回退');
  await open(page, value);
  const captured = deferred();
  const released = deferred();
  const delivered = deferred();
  let taskId = '';
  let initialVersion = 0;
  await page.route(`**/api/projects/${value.id}/tasks`, async (route) => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    const initial = await response.json() as Task;
    expect(initial.state).toBe('queued');
    taskId = initial.id; initialVersion = initial.state_version;
    captured.resolve(); await released.promise;
    await route.fulfill({ response }); delivered.resolve();
  });
  await expect(submitButton(page, 'full')).toBeEnabled();
  await submitButton(page, 'full').click();
  await captured.promise;
  await expect.poll(async () => (await taskDetail(page, value.id, taskId)).state, { timeout: 20000 }).toBe('succeeded');
  const current = await taskDetail(page, value.id, taskId);
  expect(current.state_version).toBeGreaterThan(initialVersion);
  await panel(page).getByRole('button', { name: '刷新任务', exact: true }).click();
  await expect(card(page, taskId).getByLabel('模拟任务结果', { exact: true })).toBeVisible();
  released.resolve(); await delivered.promise;
  await rendered(page);
  await expect(card(page, taskId).getByLabel('模拟任务结果', { exact: true })).toBeVisible();
  await expect(card(page, taskId)).toContainText('已完成');
  await expect(card(page, taskId).getByRole('button', { name: '取消排队', exact: true })).toHaveCount(0);
  expect((await taskList(page, value.id)).tasks).toHaveLength(1);
});

test('切换项目后迟到的任务提交与读取不会污染新项目', async ({ page }) => {
  const first = await seed(page, '任务迟到原项目');
  const second = await seed(page, '任务迟到新项目');
  await open(page, first);
  const captured = deferred();
  const released = deferred();
  const delivered = deferred();
  let originalTaskId = '';
  await page.route(`**/api/projects/${first.id}/tasks`, async (route) => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    const response = await route.fetch();
    originalTaskId = (await response.json() as Task).id;
    captured.resolve(); await released.promise;
    await route.fulfill({ response }); delivered.resolve();
  });
  await expect(submitButton(page, 'full')).toBeEnabled();
  await submitButton(page, 'full').click();
  await captured.promise;
  await page.evaluate((id) => { location.hash = `project=${id}`; }, second.id);
  await expect(page.getByRole('region', { name: '项目详情' })).toContainText('任务迟到新项目');
  await expect(panel(page)).toBeVisible();
  await page.getByLabel('武汉话草稿', { exact: true }).first().fill('新项目未保存的独立文案');
  released.resolve(); await delivered.promise;
  await rendered(page);
  await expect(panel(page).locator('article[data-task-id]')).toHaveCount(0);
  await expect(page.getByLabel('武汉话草稿', { exact: true }).first()).toHaveValue('新项目未保存的独立文案');
  await expect(submitButton(page, 'full')).toBeDisabled();
  expect((await taskList(page, second.id)).tasks).toHaveLength(0);
  const taskId = originalTaskId;
  const crossProject = await page.request.get(`/api/projects/${second.id}/tasks/${taskId}`);
  expect(crossProject.status()).toBe(404);
  const oldRead = deferred();
  const oldRelease = deferred();
  const oldDelivered = deferred();
  await page.route(`**/api/projects/${first.id}/tasks`, async (route) => {
    const response = await route.fetch();
    oldRead.resolve(); await oldRelease.promise;
    await route.fulfill({ response }); oldDelivered.resolve();
  }, { times: 1 });
  await page.evaluate((id) => { location.hash = `project=${id}`; }, first.id);
  await oldRead.promise;
  await page.evaluate((id) => { location.hash = `project=${id}`; }, second.id);
  await expect(page.getByLabel('武汉话草稿', { exact: true }).first()).toHaveValue('新项目未保存的独立文案');
  oldRelease.resolve(); await oldDelivered.promise;
  await rendered(page);
  await expect(panel(page).locator('article[data-task-id]')).toHaveCount(0);
  await expect(panel(page).getByRole('region', { name: '任务输入快照' })).toHaveCount(0);
  expect((await taskList(page, first.id)).tasks.map((task) => task.id)).toEqual([taskId]);
});
