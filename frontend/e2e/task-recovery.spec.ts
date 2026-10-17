import { expect, test, type Page } from '@playwright/test';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import type { Project } from '../src/api';
import type { TaskDetail } from '../src/taskTypes';

type Seed = { project_id: string; task_id: string };
type Fixture = { origin: string; failed: Seed; queued: Seed };
const root = fileURLToPath(new URL('../../', import.meta.url));
const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const panel = (page: Page) => page.getByRole('region', { name: '模拟生成与任务' });
const card = (page: Page, id: string) => panel(page).locator(`article[data-task-id="${id}"]`);
let fixture: Fixture;
let child: ChildProcessWithoutNullStreams | undefined;
let directory: string | undefined;
let closed: Promise<number | null> | undefined;
let errors = '';

test.beforeAll(async ({ playwright }) => {
  directory = await mkdtemp(path.join(tmpdir(), 'voice-task-recovery-'));
  child = spawn(python, [path.join(root, 'scripts/e2e_task_recovery.py'), '--data-dir', directory], {
    cwd: root, stdio: ['pipe', 'pipe', 'pipe'],
  });
  closed = new Promise((resolve) => { child!.once('close', resolve); });
  child.stderr.on('data', (chunk: Buffer) => { errors = (errors + chunk.toString()).slice(-8000); });
  const lines = createInterface({ input: child.stdout });
  try {
    fixture = await new Promise<Fixture>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`Recovery fixture startup timed out: ${errors}`)), 15000);
      child!.once('error', (error) => { clearTimeout(timer); reject(error); });
      child!.once('close', (code) => {
        clearTimeout(timer); reject(new Error(`Recovery fixture exited (${code}): ${errors}`));
      });
      lines.once('line', (line) => {
        clearTimeout(timer);
        try { resolve(JSON.parse(line) as Fixture); } catch (error) { reject(error); }
      });
    });
  } finally { lines.close(); }
  expect(fixture.origin).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
  const request = await playwright.request.newContext({ baseURL: fixture.origin });
  try {
    await expect.poll(async () => {
      try { return (await request.get('/api/health', { timeout: 1000 })).status(); } catch { return 0; }
    }, { timeout: 10000 }).toBe(200);
  } finally { await request.dispose(); }
});

test.afterAll(async () => {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    if (child && closed) {
      child.stdin.end();
      const exitCode = await Promise.race([
        closed,
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new Error(`Recovery fixture did not stop: ${errors}`)), 7000);
        }),
      ]).catch(async (error: unknown) => {
        child!.kill('SIGKILL'); await closed; throw error;
      });
      expect(exitCode).toBe(0);
    }
  } finally {
    if (timer) clearTimeout(timer);
    if (directory) await rm(directory, { recursive: true, force: true, maxRetries: 3 });
  }
});

const taskURL = (seed: Seed) => `${fixture.origin}/api/projects/${seed.project_id}/tasks/${seed.task_id}`;
async function detail(page: Page, seed: Seed) {
  const response = await page.request.get(taskURL(seed));
  expect(response.status()).toBe(200);
  return await response.json() as TaskDetail;
}
async function open(page: Page, seed: Seed) {
  await page.goto(`${fixture.origin}/#project=${seed.project_id}`);
  await expect(card(page, seed.task_id)).toBeVisible();
}

test('真实排队任务可从页面取消，关闭重开页面后取消状态与快照仍保留', async ({ page, context }) => {
  const seed = fixture.queued;
  const before = await detail(page, seed);
  expect(before.state).toBe('queued');
  await open(page, seed);
  await expect(card(page, seed.task_id)).toContainText('排队中');
  const event = page.waitForResponse((response) => response.url() === `${taskURL(seed)}/cancel`
    && response.request().method() === 'POST');
  await card(page, seed.task_id).getByRole('button', { name: '取消排队', exact: true }).click();
  const response = await event;
  expect(response.status()).toBe(200);
  const canceled = await response.json() as TaskDetail;
  expect(canceled.state).toBe('canceled');
  expect(canceled.state_version).toBeGreaterThan(before.state_version);
  expect(canceled.snapshot).toEqual(before.snapshot);
  expect(canceled.result).toBeNull();
  await expect(card(page, seed.task_id)).toContainText('已取消');
  await expect(card(page, seed.task_id).getByRole('button', { name: '取消排队', exact: true })).toHaveCount(0);
  await expect(card(page, seed.task_id).getByLabel('模拟任务结果', { exact: true })).toHaveCount(0);
  await page.close();
  const reopened = await context.newPage();
  await open(reopened, seed);
  await expect(card(reopened, seed.task_id)).toContainText('已取消');
  await card(reopened, seed.task_id).getByRole('button', { name: '查看任务快照', exact: true }).click();
  await expect(reopened.getByRole('region', { name: '任务输入快照' }))
    .toContainText(before.snapshot.confirmation.paragraphs[0].draft_text);
  expect((await detail(reopened, seed)).snapshot).toEqual(before.snapshot);
  await reopened.close();
});

test('真实适配器失败后按原输入重试，响应丢失及再次点击只保留一个子任务', async ({ page, context }) => {
  const seed = fixture.failed;
  const before = await detail(page, seed);
  expect(before.state).toBe('failed');
  expect(before.error?.code).toBe('generation_failed');
  expect(before.started_at).not.toBeNull();
  expect(before.finished_at).not.toBeNull();
  expect(before.result).toBeNull();
  await open(page, seed);
  await expect(card(page, seed.task_id)).toContainText('generation_failed');
  await card(page, seed.task_id).getByRole('button', { name: '查看任务快照', exact: true }).click();
  const snapshot = page.getByRole('region', { name: '任务输入快照' });
  await expect(snapshot).toContainText(before.snapshot.confirmation.paragraphs[0].draft_text);
  await page.getByLabel('武汉话草稿', { exact: true }).first().fill('失败之后重新保存确认的稿件，金额 456 元。');
  await page.getByRole('button', { name: '保存文案', exact: true }).click();
  await expect(page.getByRole('status', { name: '文案操作状态' })).toContainText('文案已保存');
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: '确认当前文案', exact: true }).click();
  await expect(page.getByRole('status', { name: '文案操作状态' })).toContainText('文案已确认');
  const clipResponse = await page.request.post(
    `${fixture.origin}/api/projects/${seed.project_id}/references/${before.snapshot.reference.id}/clips`,
    { data: { start_ms: 200, end_ms: 900 } },
  );
  expect(clipResponse.status()).toBe(201);
  expect((await clipResponse.json() as { id: string }).id).not.toBe(before.snapshot.reference.clip.id);
  const newerResponse = await page.request.post(`${fixture.origin}/api/projects`, { data: { name: '重试前新建的对照项目' } });
  expect(newerResponse.status()).toBe(201);
  const newer = await newerResponse.json() as Project;
  await page.reload();
  const list = page.getByRole('region', { name: '项目列表' });
  await expect(list.getByRole('link').first()).toHaveAttribute('href', `#project=${newer.id}`);
  const projectBefore = await (await page.request.get(`${fixture.origin}/api/projects/${seed.project_id}`)).json() as Project;
  const retryURL = `${taskURL(seed)}/retry`;
  let originalRequest: unknown;
  let childTask: TaskDetail | undefined;
  await page.route(retryURL, async (route) => {
    originalRequest = route.request().postDataJSON();
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    childTask = await response.json() as TaskDetail;
    await route.abort('failed');
  }, { times: 1 });
  await card(page, seed.task_id).getByRole('button', { name: '按原输入重试', exact: true }).click();
  await expect(page.getByRole('alert', { name: '任务操作错误' })).toContainText('提交结果尚未确认');
  expect(childTask).toBeDefined();
  expect(childTask!.retry_of).toBe(seed.task_id);
  expect(childTask!.state).toBe('queued');
  expect(childTask!.snapshot).toEqual(before.snapshot);
  const replayEvent = page.waitForResponse((response) => response.url() === retryURL && response.request().method() === 'POST');
  await panel(page).getByRole('button', { name: '核对并重试上次提交', exact: true }).click();
  const replayResponse = await replayEvent;
  expect(replayResponse.status()).toBe(200);
  expect(replayResponse.request().postDataJSON()).toEqual(originalRequest);
  expect((await replayResponse.json() as TaskDetail).id).toBe(childTask!.id);
  await expect(page.getByRole('status', { name: '任务操作状态' })).toContainText('重试任务已保存');
  const projectAfter = await (await page.request.get(`${fixture.origin}/api/projects/${seed.project_id}`)).json() as Project;
  expect(projectAfter.updated_at).not.toBe(projectBefore.updated_at);
  await expect(list.getByRole('link').first()).toHaveAttribute('href', `#project=${seed.project_id}`);
  await expect(list.locator(`a[href="#project=${seed.project_id}"] time`)).toHaveAttribute('datetime', projectAfter.updated_at);
  await expect(page.getByRole('region', { name: '项目详情' }).getByText('最近更新', { exact: true }).locator('..').locator('time'))
    .toHaveAttribute('datetime', projectAfter.updated_at);
  const repeatEvent = page.waitForResponse((response) => response.url() === retryURL && response.request().method() === 'POST');
  await card(page, seed.task_id).getByRole('button', { name: '按原输入重试', exact: true }).click();
  const repeatResponse = await repeatEvent;
  expect(repeatResponse.status()).toBe(200);
  expect(repeatResponse.request().postDataJSON()).not.toEqual(originalRequest);
  expect((await repeatResponse.json() as TaskDetail).id).toBe(childTask!.id);
  const tasksResponse = await page.request.get(`${fixture.origin}/api/projects/${seed.project_id}/tasks`);
  expect(tasksResponse.status()).toBe(200);
  const tasks = (await tasksResponse.json() as { tasks: TaskDetail[] }).tasks;
  expect(tasks).toHaveLength(2);
  expect(tasks.filter((task) => task.retry_of === seed.task_id)).toHaveLength(1);
  const original = await detail(page, seed);
  expect(original.state).toBe('failed');
  expect(original.error).toEqual(before.error);
  expect(original.snapshot).toEqual(before.snapshot);
  expect(original.state_version).toBe(before.state_version);
  expect(original.result).toBeNull();
  await card(page, childTask!.id).getByRole('button', { name: '查看任务快照', exact: true }).click();
  await expect(snapshot).toContainText(childTask!.id);
  await expect(snapshot).toContainText(before.snapshot.confirmation.paragraphs[0].draft_text);
  await expect(snapshot).toContainText(before.snapshot.reference.clip.id);
  await expect(snapshot).not.toContainText('失败之后重新保存确认的稿件');
  await page.close();
  const reopened = await context.newPage();
  await open(reopened, seed);
  await expect(card(reopened, seed.task_id)).toContainText('generation_failed');
  await expect(card(reopened, childTask!.id)).toContainText('排队中');
  expect((await detail(reopened, { ...seed, task_id: childTask!.id })).snapshot).toEqual(before.snapshot);
  await expect(panel(reopened).getByLabel('模拟任务结果', { exact: true })).toHaveCount(0);
  await reopened.close();
});
