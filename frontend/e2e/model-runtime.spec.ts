import { expect, test, type Page } from '@playwright/test';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import type { Project } from '../src/api';
import type { Adapter, AdapterHealth, TaskDetail } from '../src/taskTypes';
import type { ReferenceTranscript } from '../src/ReferenceTranscript';
import type { TextDocument } from '../src/TextPanel';

type Seed = { project: Project; document: TextDocument; reference_id: string; clip_id: string; transcript: ReferenceTranscript };
type Fixture = { origin: string; projects: Record<string, Seed>; model_file: string | null; directory: string };
type ModelTask = TaskDetail & {
  snapshot: TaskDetail['snapshot'] & { adapter: Adapter & { environment_fingerprint: string } };
  result: null | (NonNullable<TaskDetail['result']> & {
    frame_count: number; input_sha256: string; reference_sha256: string; config_fingerprint: string;
    model_fingerprint: string; environment_fingerprint: string; native_sample_rate: number;
  });
};
type Child = { process: ChildProcessWithoutNullStreams; closed: Promise<number | null>; directory: string; errors: string };
const root = fileURLToPath(new URL('../../', import.meta.url));
const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const children: Child[] = [];
let fixture: Fixture;
const panel = (page: Page) => page.getByRole('region', { name: '真实语音与任务', exact: true });
const editor = (page: Page) => page.getByRole('region', { name: '参考片段原话', exact: true });
const words = (page: Page) => editor(page).getByLabel('参考片段准确原话', { exact: true });
const card = (page: Page, id: string) => page.locator(`article[data-task-id="${id}"]`);
const realSubmit = (page: Page, mode: 'preview' | 'full') => panel(page).getByRole('button', {
  name: mode === 'preview' ? '提交真实短段任务' : '提交真实全文任务', exact: true,
});
const tasksURL = (seed: Seed, value = fixture) => `${value.origin}/api/projects/${seed.project.id}/tasks`;
const transcriptURL = (seed: Seed, value = fixture) => `${value.origin}/api/projects/${seed.project.id}/references/${seed.reference_id}/clips/${seed.clip_id}/transcript`;
const control = (name: string) => path.join(fixture.directory, 'control', name);

async function launch(unconfigured = false): Promise<Fixture> {
  const directory = await mkdtemp(path.join(tmpdir(), 'voice-model-browser-'));
  const process = spawn(python, [path.join(root, 'scripts/e2e_model_generation.py'), '--directory', directory,
    ...(unconfigured ? ['--unconfigured'] : [])], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'] });
  const state: Child = { process, directory, errors: '', closed: new Promise((resolve) => process.once('close', resolve)) };
  children.push(state);
  process.stderr.on('data', (chunk: Buffer) => { state.errors = (state.errors + chunk.toString()).slice(-12000); });
  const lines = createInterface({ input: process.stdout });
  try {
    const value = await new Promise<Omit<Fixture, 'directory'>>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`Model fixture startup timeout: ${state.errors}`)), 15000);
      process.once('error', (error) => { clearTimeout(timer); reject(error); });
      process.once('close', (code) => { clearTimeout(timer); reject(new Error(`Model fixture exited ${code}: ${state.errors}`)); });
      lines.once('line', (line) => {
        clearTimeout(timer);
        try { resolve(JSON.parse(line) as Omit<Fixture, 'directory'>); } catch (error) { reject(error); }
      });
    });
    expect(value.origin).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
    return { ...value, directory };
  } finally { lines.close(); }
}

test.beforeAll(async ({ playwright }) => {
  fixture = await launch();
  const client = await playwright.request.newContext({ baseURL: fixture.origin });
  try {
    await expect.poll(async () => {
      try { return (await client.get('/api/health', { timeout: 1000 })).status(); } catch { return 0; }
    }).toBe(200);
  } finally { await client.dispose(); }
});

test.afterEach(async () => {
  if (fixture) for (const name of ['hold', 'fail-next']) await rm(control(name), { force: true });
});

test.afterAll(async () => {
  const failures: unknown[] = [];
  for (const child of children.splice(0)) {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      child.process.stdin.end();
      const code = await Promise.race([child.closed, new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error(`Model fixture shutdown timeout: ${child.errors}`)), 10000);
      })]).catch(async (error: unknown) => { child.process.kill('SIGKILL'); await child.closed; throw error; });
      expect(code, child.errors).toBe(0);
    } catch (error) { failures.push(error); }
    finally {
      if (timer) clearTimeout(timer);
      await rm(child.directory, { recursive: true, force: true, maxRetries: 3 });
    }
  }
  if (failures.length) throw new AggregateError(failures, 'Model fixture cleanup failed');
});

async function health(page: Page, value = fixture) {
  const response = await page.request.get(`${value.origin}/api/generation-adapters/cosyvoice3-local/health`);
  expect(response.status()).toBe(200);
  return await response.json() as AdapterHealth;
}

async function open(page: Page, seed: Seed, value = fixture) {
  await page.goto(`${value.origin}/#project=${seed.project.id}`);
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('cosyvoice3-local');
  await expect(words(page)).toBeEnabled();
  await expect(words(page)).toHaveValue(seed.transcript.text);
}

async function saveWords(page: Page, seed: Seed) {
  const pending = page.waitForResponse((response) => response.url() === transcriptURL(seed) && response.request().method() === 'PUT');
  await editor(page).getByRole('button', { name: '保存片段原话', exact: true }).click();
  const response = await pending;
  expect(response.status()).toBe(200);
  await expect(page.getByRole('status', { name: '片段原话状态' })).toContainText('片段原话已保存');
  return await response.json() as ReferenceTranscript;
}

async function detail(page: Page, seed: Seed, id: string) {
  const response = await page.request.get(`${tasksURL(seed)}/${id}`);
  expect(response.status()).toBe(200);
  return await response.json() as ModelTask;
}

async function submit(page: Page, seed: Seed, mode: 'preview' | 'full') {
  const pending = page.waitForResponse((response) => response.url() === tasksURL(seed) && response.request().method() === 'POST');
  await realSubmit(page, mode).click();
  const response = await pending;
  expect(response.status()).toBe(201);
  return await response.json() as ModelTask;
}

async function completed(page: Page, seed: Seed, id: string) {
  await expect.poll(async () => (await detail(page, seed, id)).state).toBe('succeeded');
  await expect(card(page, id)).toContainText('已完成');
  return await detail(page, seed, id);
}

function canonical(value: unknown): string {
  function sorted(input: unknown): unknown {
    if (Array.isArray(input)) return input.map(sorted);
    if (input !== null && typeof input === 'object') return Object.fromEntries(
      Object.entries(input).sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0).map(([key, item]) => [key, sorted(item)]),
    );
    return input;
  }
  return JSON.stringify(sorted(value)).replace(/[\u007f-\uffff]/g, (letter) => `\\u${letter.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

test('实际未配置与配置文件失效会阻断真实提交，配置检查不冒充已加载', async ({ page }) => {
  const missing = await launch(true);
  await open(page, missing.projects.configuration, missing);
  const absent = await health(page, missing);
  expect(absent).toMatchObject({ configured: false, configuration_valid: false, execution_available: false, model_loaded: false });
  await expect(page.getByLabel('真实模型状态', { exact: true })).toContainText('模型未配置');
  await expect(realSubmit(page, 'full')).toBeDisabled();
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('simulated-tone');
  await expect(page.getByRole('button', { name: '提交模拟全文任务', exact: true })).toBeEnabled();
  const seed = fixture.projects.configuration;
  await open(page, seed);
  expect(await health(page)).toMatchObject({ configured: true, configuration_valid: true, execution_available: true, model_loaded: false, state: 'ready' });
  await expect(page.getByLabel('真实模型状态', { exact: true })).toContainText('尚无已加载成功的证明');
  await expect(realSubmit(page, 'full')).toBeEnabled();
  const modelFile = fixture.model_file!;
  await rename(modelFile, `${modelFile}.held`);
  try {
    await panel(page).getByRole('button', { name: '刷新参考与组件', exact: true }).click();
    await expect(page.getByLabel('真实模型状态', { exact: true })).toContainText('模型配置不可用');
    expect(await health(page)).toMatchObject({ configuration_valid: false, execution_available: false, model_loaded: false });
    await expect(realSubmit(page, 'preview')).toBeDisabled();
    await expect(realSubmit(page, 'full')).toBeDisabled();
  } finally { await rename(`${modelFile}.held`, modelFile); }
  await panel(page).getByRole('button', { name: '刷新参考与组件', exact: true }).click();
  await expect(realSubmit(page, 'full')).toBeEnabled();
  expect((await health(page)).model_loaded).toBe(false);
});

test('入口真实可调用时，未保存与已保存空原话均禁提交，保存非空稿后恢复', async ({ page }) => {
  const seed = fixture.projects.gates;
  await open(page, seed);
  expect((await health(page)).execution_available).toBe(true);
  await expect(realSubmit(page, 'preview')).toBeEnabled();
  await expect(realSubmit(page, 'full')).toBeEnabled();
  await words(page).fill('仍未保存的准确原话');
  await expect(realSubmit(page, 'preview')).toBeDisabled();
  await expect(realSubmit(page, 'full')).toBeDisabled();
  await words(page).fill('');
  await saveWords(page, seed);
  await expect(realSubmit(page, 'preview')).toBeDisabled();
  await expect(realSubmit(page, 'full')).toBeDisabled();
  expect((await health(page)).execution_available).toBe(true);
  await words(page).fill('重新填写仍未保存的准确原话');
  await expect(realSubmit(page, 'full')).toBeDisabled();
  const saved = await saveWords(page, seed);
  await expect(realSubmit(page, 'preview')).toBeEnabled();
  await expect(realSubmit(page, 'full')).toBeEnabled();
  const task = await submit(page, seed, 'preview');
  const result = await completed(page, seed, task.id);
  expect(result.is_simulated).toBe(false);
  expect(result.snapshot.reference.transcript).toEqual(saved);
  expect(result.snapshot.paragraph_ids).toHaveLength(1);
  expect(result.result!.frame_count).toBe(6000);
});

test('真实适配器替身子进程收到固定全文原话参考，播放下载校验字节并保留模拟标识', async ({ page }) => {
  const seed = fixture.projects.playback;
  await open(page, seed);
  await writeFile(control('hold'), 'Browser-controlled fixture barrier');
  let task!: ModelTask;
  try {
    task = await submit(page, seed, 'full');
    await expect.poll(async () => {
      try { return JSON.parse(await readFile(control(`${task.id}.json`), 'utf8')) as unknown; } catch { return null; }
    }).not.toBeNull();
    expect((await detail(page, seed, task.id)).state).toBe('running');
    expect((await health(page)).model_loaded).toBe(true);
    const response = await page.request.put(transcriptURL(seed), { data: {
      expected_revision: seed.transcript.revision, expected_clip_sha256: seed.transcript.clip_sha256,
      text: '入队之后的另一版原话，不能进入旧任务',
    } });
    expect(response.status()).toBe(200);
    const clip = await page.request.post(`${fixture.origin}/api/projects/${seed.project.id}/references/${seed.reference_id}/clips`, {
      data: { start_ms: 200, end_ms: 900 },
    });
    expect(clip.status()).toBe(201);
    expect((await clip.json() as { id: string }).id).not.toBe(seed.clip_id);
    const saved = await page.request.put(`${fixture.origin}/api/projects/${seed.project.id}/text`, { data: {
      expected_revision: seed.document.revision, source_language: seed.document.source_language,
      target_language: seed.document.target_language, register: seed.document.register, terms: seed.document.terms,
      paragraphs: seed.document.paragraphs.map((paragraph) => ({ ...paragraph, draft_text: '入队后修改的文案不能替换旧任务' })),
    } });
    expect(saved.status()).toBe(200);
    const confirmed = await page.request.post(`${fixture.origin}/api/projects/${seed.project.id}/text/confirm`, {
      data: { expected_revision: (await saved.json() as TextDocument).revision, acknowledged: true },
    });
    expect(confirmed.status()).toBe(200);
  } finally { await rm(control('hold'), { force: true }); }
  const result = await completed(page, seed, task.id);
  expect(result.snapshot).toEqual(task.snapshot);
  expect(result.completed_paragraphs).toBe(2);
  expect(result.result!.frame_count).toBe(12000);
  const evidence = JSON.parse(await readFile(control(`${task.id}.json`), 'utf8')) as {
    scope: string; texts: string[]; transcript: ReferenceTranscript; reference_sha256: string; snapshot: unknown;
  };
  expect(evidence.scope).toBe('synthetic bridge software fixture');
  expect(evidence.texts).toEqual(seed.document.paragraphs.map((paragraph) => paragraph.draft_text));
  expect(evidence.transcript).toEqual(seed.transcript);
  expect(evidence.snapshot).toEqual(task.snapshot);
  expect(evidence.reference_sha256).toBe(task.snapshot.reference.clip_sha256);
  expect(result.result).toMatchObject({
    reference_sha256: task.snapshot.reference.clip_sha256,
    config_fingerprint: task.snapshot.adapter.config_fingerprint,
    model_fingerprint: task.snapshot.adapter.model_fingerprint,
    environment_fingerprint: task.snapshot.adapter.environment_fingerprint,
    native_sample_rate: 24000,
    input_sha256: createHash('sha256').update(canonical(task.snapshot)).digest('hex'),
  });
  await card(page, task.id).getByRole('button', { name: '查看任务快照', exact: true }).click();
  const snapshot = page.getByRole('region', { name: '任务输入快照', exact: true });
  await expect(snapshot).toContainText(seed.transcript.text);
  await expect(snapshot).toContainText(seed.document.paragraphs[0].draft_text);
  await expect(snapshot).toContainText(seed.clip_id);
  await expect(snapshot).not.toContainText('入队后修改的文案不能替换旧任务');
  await snapshot.getByText('查看固定模型与配置摘要', { exact: true }).click();
  await expect(snapshot).toContainText(task.snapshot.adapter.config_fingerprint!);
  const audio = card(page, task.id).getByLabel('真实语音任务结果', { exact: true });
  await audio.evaluate(async (element: HTMLAudioElement) => { await element.play(); });
  await expect.poll(() => audio.evaluate((element: HTMLAudioElement) => element.currentTime)).toBeGreaterThan(0);
  const streamed = await page.request.get(`${tasksURL(seed)}/${task.id}/audio`);
  expect(streamed.status()).toBe(200);
  expect(streamed.headers()['content-type']).toContain('audio/wav');
  const bytes = await streamed.body();
  expect(bytes.subarray(0, 4).toString()).toBe('RIFF');
  expect(bytes.subarray(8, 12).toString()).toBe('WAVE');
  expect(bytes.length).toBe(result.result!.byte_size);
  expect(createHash('sha256').update(bytes).digest('hex')).toBe(result.result!.sha256);
  const downloading = page.waitForEvent('download');
  await card(page, task.id).getByRole('link', { name: '下载生成语音', exact: true }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toBe(result.result!.filename);
  expect(download.suggestedFilename()).toMatch(/^generated-.*\.wav$/);
  expect(await readFile((await download.path())!)).toEqual(bytes);
  expect((await health(page)).model_loaded).toBe(false);
  await page.reload();
  await expect(card(page, task.id)).toContainText('真实语音全文任务');
  const simulated = page.waitForResponse((response) => response.url() === tasksURL(seed) && response.request().method() === 'POST');
  await page.getByRole('button', { name: '提交模拟全文任务', exact: true }).click();
  const simulatedResponse = await simulated;
  expect(simulatedResponse.status()).toBe(201);
  const simulatedTask = await simulatedResponse.json() as TaskDetail;
  await completed(page, seed, simulatedTask.id);
  await expect(card(page, simulatedTask.id)).toContainText('模拟全文任务');
  await expect(card(page, simulatedTask.id).getByLabel('模拟任务结果', { exact: true })).toBeVisible();
  await expect(card(page, task.id).getByLabel('真实语音任务结果', { exact: true })).toBeVisible();
});

test('真实任务 POST 成功但响应丢失后重放同一请求，只保留一个原输入任务', async ({ page }) => {
  const seed = fixture.projects.replay;
  await open(page, seed);
  let submitted: ModelTask | undefined;
  let payload: unknown;
  await page.route(tasksURL(seed), async (route) => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    payload = route.request().postDataJSON();
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    submitted = await response.json() as ModelTask;
    await route.abort('failed');
  }, { times: 1 });
  await realSubmit(page, 'full').click();
  await expect(page.getByRole('alert', { name: '任务操作错误' })).toContainText('提交结果尚未确认');
  expect(submitted).toBeDefined();
  const replay = page.waitForResponse((response) => response.url() === tasksURL(seed) && response.request().method() === 'POST');
  await panel(page).getByRole('button', { name: '核对并重试上次提交', exact: true }).click();
  const response = await replay;
  expect(response.status()).toBe(200);
  expect(response.request().postDataJSON()).toEqual(payload);
  expect((await response.json() as ModelTask).id).toBe(submitted!.id);
  const result = await completed(page, seed, submitted!.id);
  expect(result.snapshot).toEqual(submitted!.snapshot);
  const all = await page.request.get(tasksURL(seed));
  expect(all.status()).toBe(200);
  expect((await all.json() as { tasks: TaskDetail[] }).tasks).toHaveLength(1);
});

test('真实适配器替身推理失败后原快照重试执行成功，旧失败与新结果均保留', async ({ page }) => {
  const seed = fixture.projects.failure;
  await open(page, seed);
  await writeFile(control('fail-next'), 'One actual runner failure');
  const task = await submit(page, seed, 'full');
  await expect.poll(async () => (await detail(page, seed, task.id)).state).toBe('failed');
  const failed = await detail(page, seed, task.id);
  expect(failed.error?.code).toBe('model_inference_failed');
  expect(failed.result).toBeNull();
  await expect(card(page, task.id)).toContainText('model_inference_failed');
  const changed = await page.request.put(transcriptURL(seed), { data: {
    expected_revision: seed.transcript.revision, expected_clip_sha256: seed.transcript.clip_sha256,
    text: '失败之后更新的原话不能替换重试快照',
  } });
  expect(changed.status()).toBe(200);
  const pending = page.waitForResponse((response) => response.url() === `${tasksURL(seed)}/${task.id}/retry`
    && response.request().method() === 'POST');
  await card(page, task.id).getByRole('button', { name: '按原输入重试', exact: true }).click();
  const response = await pending;
  expect(response.status()).toBe(201);
  const child = await response.json() as ModelTask;
  expect(child.retry_of).toBe(task.id);
  const result = await completed(page, seed, child.id);
  expect(result.snapshot).toEqual(failed.snapshot);
  expect(result.snapshot.reference.transcript).toEqual(seed.transcript);
  expect(result.result!.reference_sha256).toBe(failed.snapshot.reference.clip_sha256);
  const audio = await page.request.get(`${tasksURL(seed)}/${child.id}/audio`);
  expect(audio.status()).toBe(200);
  expect(createHash('sha256').update(await audio.body()).digest('hex')).toBe(result.result!.sha256);
  expect(await detail(page, seed, task.id)).toEqual(failed);
  await expect(card(page, task.id)).toContainText('model_inference_failed');
  await expect(card(page, child.id)).toContainText('已完成');
});
