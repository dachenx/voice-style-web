import { expect, test, type Page } from '@playwright/test';
import type { Project } from '../src/api';
import type { TextDocument } from '../src/TextPanel';

const workbench = (page: Page) => page.getByRole('region', { name: '文案工作台' });
const operationStatus = (page: Page) => page.getByRole('status', { name: '文案操作状态' });
const historyButton = (page: Page, revision: number) => page.getByRole('button', {
  name: new RegExp(`查看确认版本 ${revision} ·`),
});

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

async function heldRead(page: Page, url: string, fail = false) {
  const captured = deferred();
  const released = deferred();
  const delivered = deferred();
  await page.route(url, async (route) => {
    const response = await route.fetch();
    captured.resolve();
    await released.promise;
    if (fail) {
      await route.fulfill({ status: 503, contentType: 'application/json',
        body: JSON.stringify({ error: { message: '旧历史请求读取失败' } }) });
    } else {
      await route.fulfill({ response });
    }
    delivered.resolve();
  }, { times: 1 });
  return { captured: captured.promise, release: released.resolve, delivered: delivered.promise };
}

async function rendered(page: Page) {
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

async function project(page: Page, name: string) {
  const response = await page.request.post('/api/projects', { data: { name } });
  expect(response.ok()).toBe(true);
  return await response.json() as Project;
}

async function readProject(page: Page, id: string) {
  const response = await page.request.get(`/api/projects/${id}`);
  expect(response.ok()).toBe(true);
  return await response.json() as Project;
}

async function readDocument(page: Page, id: string) {
  const response = await page.request.get(`/api/projects/${id}/text`);
  expect(response.ok()).toBe(true);
  return await response.json() as TextDocument;
}

async function open(page: Page, id: string) {
  await page.goto(`/#project=${id}`);
  await expect(workbench(page).getByRole('button', { name: '添加段落', exact: true })).toBeVisible();
}

async function fill(page: Page, draft = '第一版人工草稿') {
  await page.getByRole('button', { name: '添加段落', exact: true }).click();
  await page.getByLabel('普通话原稿', { exact: true }).fill('第一版普通话原稿');
  await page.getByLabel('武汉话草稿', { exact: true }).fill(draft);
}

async function save(page: Page) {
  await page.getByRole('button', { name: '保存文案', exact: true }).click();
  await expect(operationStatus(page)).toContainText('文案已保存');
}

async function confirm(page: Page) {
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: '确认当前文案', exact: true }).click();
  await expect(operationStatus(page)).toContainText('文案已确认');
  await expect(workbench(page)).toContainText('文案已准备好；语音生成将在后续模块开放。');
}

async function updatedView(page: Page, value: Project) {
  const list = page.getByRole('region', { name: '项目列表' });
  await expect(list.getByRole('link').first()).toHaveAttribute('href', `#project=${value.id}`);
  await expect(list.locator(`a[href="#project=${value.id}"] time`)).toHaveAttribute('datetime', value.updated_at);
  const detail = page.getByRole('region', { name: '项目详情' });
  await expect(detail.getByRole('heading', { name: value.name, exact: true })).toBeVisible();
  await expect(detail.getByText('最近更新', { exact: true }).locator('..').locator('time'))
    .toHaveAttribute('datetime', value.updated_at);
}

async function confirmedProject(page: Page, name: string) {
  const value = await project(page, name);
  await open(page, value.id);
  await fill(page);
  await save(page);
  await confirm(page);
  return value;
}

test('保存和首次确认同步真实项目时间与顺序，项目信息提示不覆盖未保存文案', async ({ page }) => {
  const first = await project(page, '文案更新排序 A');
  const second = await project(page, '文案更新排序 B');
  await open(page, first.id);
  await expect(page.getByRole('region', { name: '项目列表' }).getByRole('link').first())
    .toHaveAttribute('href', `#project=${second.id}`);
  await fill(page);
  const detail = page.getByRole('region', { name: '项目详情' });
  await expect(detail).toContainText('项目信息已保存');
  await expect(workbench(page)).toContainText('有未保存修改，请先保存，再核对确认。');
  await expect(page.getByRole('button', { name: '保存文案', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: '确认当前文案', exact: true })).toBeDisabled();
  await save(page);
  const saved = await readProject(page, first.id);
  expect(saved.updated_at).not.toBe(first.updated_at);
  await updatedView(page, saved);

  const third = await project(page, '首次确认前的新项目');
  await page.reload();
  await expect(page.getByRole('region', { name: '项目列表' }).getByRole('link').first())
    .toHaveAttribute('href', `#project=${third.id}`);
  let historyReads = 0;
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === `/api/projects/${first.id}/text/confirmations`) historyReads += 1;
  });
  await confirm(page);
  const confirmed = await readProject(page, first.id);
  expect(confirmed.updated_at).not.toBe(saved.updated_at);
  await updatedView(page, confirmed);
  expect(historyReads).toBe(0);
  await expect(historyButton(page, 1)).toHaveCount(0);
  await page.getByRole('button', { name: '查看确认历史', exact: true }).click();
  await expect(historyButton(page, 1)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('文案保存和确认后的最新列表与详情不被旧读取覆盖', async ({ page }) => {
  const first = await project(page, '迟到项目信息 A');
  for (const kind of ['save', 'confirm'] as const) {
    await project(page, `迟到项目信息 B ${kind}`);
    const old = await readProject(page, first.id);
    const oldList = await heldRead(page, '**/api/projects');
    const oldDetail = await heldRead(page, `**/api/projects/${first.id}`);
    if (kind === 'save') await open(page, first.id);
    else await page.reload();
    await Promise.all([oldList.captured, oldDetail.captured]);
    if (kind === 'save') { await fill(page); await save(page); }
    else await confirm(page);
    const current = await readProject(page, first.id);
    expect(current.updated_at).not.toBe(old.updated_at);
    await updatedView(page, current);
    oldList.release(); oldDetail.release();
    await Promise.all([oldList.delivered, oldDetail.delivered]);
    await rendered(page);
    await updatedView(page, current);
    await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveValue('第一版人工草稿');
  }
});

test('历史首次读取尚在途中也会在确认后刷新，迟到空列表不能抹掉新版本', async ({ page }) => {
  const value = await project(page, '历史首次读取与确认交错');
  await open(page, value.id);
  await fill(page);
  await save(page);
  const old = await heldRead(page, `**/api/projects/${value.id}/text/confirmations`);
  await page.getByRole('button', { name: '查看确认历史', exact: true }).click();
  await old.captured;
  await confirm(page);
  await expect(historyButton(page, 1)).toBeVisible();
  old.release(); await old.delivered;
  await rendered(page);
  await expect(historyButton(page, 1)).toHaveCount(1);
  await expect(workbench(page).getByText('暂无确认记录。', { exact: true })).toHaveCount(0);
  await expect(workbench(page).getByRole('alert')).toHaveCount(0);
});

test('新确认刷新已打开历史并保留旧快照，列表刷新与快照读取互不覆盖', async ({ page }) => {
  const value = await confirmedProject(page, '历史与只读快照独立');
  const original = (await readDocument(page, value.id)).latest_confirmation!;
  await page.getByRole('button', { name: '查看确认历史', exact: true }).click();
  await historyButton(page, 1).click();
  const snapshot = page.getByRole('region', { name: '确认快照' });
  await expect(snapshot).toContainText('第一版人工草稿');
  await page.getByLabel('武汉话草稿', { exact: true }).fill('第二版人工草稿');
  await save(page);
  await confirm(page);
  await expect(historyButton(page, 2)).toBeVisible();
  await expect(snapshot).toContainText('确认版本 1（只读）');
  await expect(snapshot).toContainText('第一版人工草稿');
  const second = (await readDocument(page, value.id)).latest_confirmation!;
  const pendingSnapshot = await heldRead(page, `**/api/projects/${value.id}/text/confirmations/${second.id}`);
  await historyButton(page, 2).click();
  await pendingSnapshot.captured;
  await page.getByLabel('武汉话草稿', { exact: true }).fill('第三版人工草稿');
  await save(page);
  await confirm(page);
  await expect(historyButton(page, 3)).toBeVisible();
  await expect(snapshot).toContainText('确认版本 1（只读）');
  pendingSnapshot.release(); await pendingSnapshot.delivered;
  await expect(snapshot).toContainText('确认版本 2（只读）');
  await expect(snapshot).toContainText('第二版人工草稿');
  await expect(historyButton(page, 3)).toBeVisible();
  const stored = await page.request.get(`/api/projects/${value.id}/text/confirmations/${original.id}`);
  expect(stored.ok()).toBe(true);
  expect(await stored.json()).toEqual(original);
});

test('历史自动刷新失败不否定确认成功，重试恢复且不重复确认或清除快照', async ({ page }) => {
  const value = await confirmedProject(page, '历史刷新失败恢复');
  await page.getByRole('button', { name: '查看确认历史', exact: true }).click();
  await historyButton(page, 1).click();
  const snapshot = page.getByRole('region', { name: '确认快照' });
  await expect(snapshot).toContainText('第一版人工草稿');
  await page.getByLabel('武汉话草稿', { exact: true }).fill('刷新失败时已确认的新稿');
  await save(page);
  await page.route(`**/api/projects/${value.id}/text/confirmations`, async (route) => {
    await route.fulfill({ status: 503, contentType: 'application/json',
      body: JSON.stringify({ error: { message: '历史暂时不可读取' } }) });
  }, { times: 1 });
  let confirmations = 0;
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === `/api/projects/${value.id}/text/confirm`) confirmations += 1;
  });
  await confirm(page);
  await expect(workbench(page).getByRole('alert', { name: '确认历史状态' })).toContainText('历史暂时不可读取');
  await expect(operationStatus(page)).toContainText('文案已确认');
  await expect(snapshot).toContainText('确认版本 1（只读）');
  await expect(historyButton(page, 1)).toBeVisible();
  const confirmed = await readDocument(page, value.id);
  expect(confirmed.confirmation_current).toBe(true);
  expect(confirmed.latest_confirmation?.revision).toBe(2);
  await page.getByRole('button', { name: '查看确认历史', exact: true }).click();
  await expect(historyButton(page, 2)).toBeVisible();
  await expect(workbench(page).getByRole('alert', { name: '确认历史状态' })).toHaveCount(0);
  await expect(snapshot).toContainText('确认版本 1（只读）');
  expect((await readDocument(page, value.id)).latest_confirmation).toEqual(confirmed.latest_confirmation);
  const history = await (await page.request.get(`/api/projects/${value.id}/text/confirmations`)).json() as { confirmations: unknown[] };
  expect(history.confirmations).toHaveLength(2);
  expect(confirmations).toBe(1);
});

test('旧历史失败迟到不能污染确认后的最新历史或成功提示', async ({ page }) => {
  const value = await project(page, '迟到历史失败隔离');
  await open(page, value.id);
  await fill(page);
  await save(page);
  const old = await heldRead(page, `**/api/projects/${value.id}/text/confirmations`, true);
  await page.getByRole('button', { name: '查看确认历史', exact: true }).click();
  await old.captured;
  await confirm(page);
  await expect(historyButton(page, 1)).toBeVisible();
  old.release(); await old.delivered;
  await rendered(page);
  await expect(workbench(page).getByRole('alert')).toHaveCount(0);
  await expect(historyButton(page, 1)).toBeVisible();
  await expect(operationStatus(page)).toContainText('文案已确认');
});

test('切换项目后迟到历史列表与快照不污染新项目输入及状态', async ({ page }) => {
  const first = await confirmedProject(page, '历史切换 A');
  const confirmation = (await readDocument(page, first.id)).latest_confirmation!;
  await page.getByRole('button', { name: '查看确认历史', exact: true }).click();
  await expect(historyButton(page, 1)).toBeVisible();
  const oldSnapshot = await heldRead(page, `**/api/projects/${first.id}/text/confirmations/${confirmation.id}`);
  await historyButton(page, 1).click();
  await oldSnapshot.captured;
  const oldList = await heldRead(page, `**/api/projects/${first.id}/text/confirmations`);
  await page.getByRole('button', { name: '查看确认历史', exact: true }).click();
  await oldList.captured;
  const second = await project(page, '历史切换 B');
  await page.evaluate((id) => { location.hash = `project=${id}`; }, second.id);
  await expect(page.getByRole('region', { name: '项目详情' })).toContainText(second.name);
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveCount(0);
  await fill(page, 'B 项目必须保留的未保存草稿');
  oldSnapshot.release(); oldList.release();
  await Promise.all([oldSnapshot.delivered, oldList.delivered]);
  await rendered(page);
  await expect(page.getByLabel('武汉话草稿', { exact: true })).toHaveValue('B 项目必须保留的未保存草稿');
  await expect(page.getByRole('region', { name: '项目详情' })).toContainText(second.name);
  await expect(page.getByRole('region', { name: '确认快照' })).toHaveCount(0);
  await expect(historyButton(page, 1)).toHaveCount(0);
  await expect(operationStatus(page)).not.toContainText('文案已确认');
  await expect(workbench(page).getByRole('alert')).toHaveCount(0);
  await expect(workbench(page)).toContainText('有未保存修改，请先保存，再核对确认。');
});
