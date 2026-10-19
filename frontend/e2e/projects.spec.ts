import { expect, test } from './advancedFixture';
import type { Project } from '../src/api';

test('迟到的旧列表不能覆盖创建和重命名后的项目', async ({ page }, testInfo) => {
  const name = `慢请求 ${testInfo.project.name}-${Date.now()}`;
  let release!: () => void;
  let captured!: () => void;
  let delivered!: () => void;
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  const snapshotCaptured = new Promise<void>((resolve) => { captured = resolve; });
  const responseDelivered = new Promise<void>((resolve) => { delivered = resolve; });
  await page.route('**/api/projects', async (route) => {
    const response = await route.fetch();
    captured();
    await waiting;
    await route.fulfill({ response });
    delivered();
  }, { times: 1 });
  await page.goto('/');
  await snapshotCaptured;
  await page.getByRole('button', { name: '新建项目' }).click();
  await page.getByLabel('项目名称', { exact: true }).fill(name);
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByRole('region', { name: '项目详情' }).getByRole('heading', { name, exact: true })).toBeVisible();
  release();
  await responseDelivered;
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.getByRole('region', { name: '项目列表' }).getByRole('heading', { name, exact: true })).toBeVisible();

  let releaseRename!: () => void;
  let capturedRename!: () => void;
  let deliveredRename!: () => void;
  const waitingRename = new Promise<void>((resolve) => { releaseRename = resolve; });
  const snapshotRename = new Promise<void>((resolve) => { capturedRename = resolve; });
  const responseRename = new Promise<void>((resolve) => { deliveredRename = resolve; });
  await page.route('**/api/projects', async (route) => {
    const response = await route.fetch();
    capturedRename();
    await waitingRename;
    await route.fulfill({ response });
    deliveredRename();
  }, { times: 1 });
  await page.reload();
  await snapshotRename;
  await page.getByRole('button', { name: '重命名', exact: true }).click();
  await page.getByLabel('项目名称', { exact: true }).fill(`${name} 已修改`);
  await page.getByRole('button', { name: '保存名称' }).click();
  await expect(page.getByRole('region', { name: '项目详情' }).getByRole('heading', { name: `${name} 已修改`, exact: true })).toBeVisible();
  releaseRename();
  await responseRename;
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.getByRole('region', { name: '项目列表' }).getByRole('heading', { name: `${name} 已修改`, exact: true })).toBeVisible();
});

test('创建、重命名、刷新和重新打开浏览器后仍能读取项目', async ({ page, browser }, testInfo) => {
  const suffix = `${testInfo.project.name}-${Date.now()}`;
  const name = `武汉街巷 ${suffix}`;
  const renamed = `街巷开场白 ${suffix}`;
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await expect(page.getByText('本地服务已连接')).toBeVisible();
  await page.getByRole('button', { name: '新建项目' }).click();
  await page.getByLabel('项目名称', { exact: true }).fill(`  ${name}  `);
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  const detail = page.getByRole('region', { name: '项目详情' });
  await expect(detail.getByRole('heading', { name, exact: true })).toBeVisible();
  const id = new URL(page.url()).hash.slice('#project='.length);
  const created = await (await page.request.get(`/api/projects/${id}`)).json() as Project;
  expect(created.name).toBe(name);
  await page.getByRole('button', { name: '重命名', exact: true }).click();
  await page.getByLabel('项目名称', { exact: true }).fill(renamed);
  await page.getByRole('button', { name: '保存名称' }).click();
  await expect(detail.getByRole('heading', { name: renamed, exact: true })).toBeVisible();
  await page.reload();
  await expect(detail.getByRole('heading', { name: renamed, exact: true })).toBeVisible();
  const context = await browser.newContext();
  const reopened = await context.newPage();
  await reopened.goto(page.url());
  await expect(reopened.getByRole('region', { name: '项目详情' }).getByRole('heading', { name: renamed })).toBeVisible();
  await context.close();
  expect(errors).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('空白、超长与控制字符名称被拦截；键盘可以取消', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '新建项目' }).click();
  const input = page.getByLabel('项目名称', { exact: true });
  await expect(input).toBeFocused();
  await input.fill('   ');
  await expect(page.getByRole('button', { name: '创建项目', exact: true })).toBeDisabled();
  await input.fill('声'.repeat(81));
  await expect(page.getByText('项目名称最多 80 个字。')).toBeVisible();
  await expect(page.getByRole('button', { name: '创建项目', exact: true })).toBeDisabled();
  await input.fill('项目\u0001名称');
  await expect(page.getByText('项目名称不能包含换行或控制字符。')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await expect(page.getByRole('button', { name: '新建项目' })).toBeFocused();
});

test('保存失败保留输入，重试后才显示成功', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '新建项目' }).click();
  await page.getByLabel('项目名称', { exact: true }).fill('网络失败后重试');
  await page.route('**/api/projects', async (route) => {
    if (route.request().method() === 'POST') await route.abort();
    else await route.continue();
  });
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('暂时无法连接');
  await expect(page.getByLabel('项目名称', { exact: true })).toHaveValue('网络失败后重试');
  await expect(page.getByRole('status', { name: '项目操作状态' })).not.toContainText('已创建');
  await page.unroute('**/api/projects');
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await expect(page.getByRole('status', { name: '项目操作状态' })).toContainText('已创建');
});

test('列表失败显示错误与重试，恢复后读取真实数据', async ({ page }) => {
  await page.route('**/api/projects', (route) => route.fulfill({
    status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'storage_unavailable', message: '本地存储暂不可用' } }),
  }));
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('本地存储暂不可用');
  await expect(page.getByText('连接异常')).toBeVisible();
  await expect(page.getByText('暂无活跃项目')).not.toBeVisible();
  await page.unroute('**/api/projects');
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await expect(page.getByText('本地服务已连接')).toBeVisible();
  await expect(page.getByRole('alert')).not.toBeVisible();
});

test('不存在的项目链接显示明确错误且可以返回列表', async ({ page }) => {
  await page.goto('/#project=00000000-0000-4000-8000-000000000000');
  await expect(page.getByRole('region', { name: '项目详情' }).getByRole('alert')).toContainText('不存在');
  await page.getByRole('link', { name: '关闭项目详情' }).click();
  await expect(page.getByRole('region', { name: '项目详情' })).not.toBeVisible();
  await expect(page.getByRole('button', { name: '新建项目' })).toBeVisible();
});


test('空分类有准确说明和可用入口，读取失败不显示空态', async ({ page }) => {
  let failed = false;
  await page.route('**/api/projects*', async (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    await route.fulfill({ status: failed ? 503 : 200, contentType: 'application/json',
      body: JSON.stringify(failed ? { error: { message: '测试列表读取失败' } } : { projects: [] }) });
  });
  await page.goto('/');
  await expect(page.getByRole('region', { name: '使用流程指南' })).toContainText('这是操作流程');
  const list = page.getByRole('region', { name: '项目列表' });
  await expect(list.getByRole('heading', { name: '暂无活跃项目' })).toBeVisible();
  await list.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await page.getByRole('button', { name: '已归档', exact: true }).click();
  await expect(list.getByRole('heading', { name: '暂无归档项目' })).toBeVisible();
  await expect(list.getByRole('button', { name: '创建项目', exact: true })).toHaveCount(0);
  await list.getByRole('button', { name: '查看活跃项目' }).click();
  await expect(list.getByRole('heading', { name: '暂无活跃项目' })).toBeVisible();
  failed = true;
  await page.getByRole('button', { name: '刷新项目列表' }).click();
  await expect(page.getByRole('alert')).toContainText('测试列表读取失败');
  await expect(list.getByText('暂无活跃项目')).toHaveCount(0);
});
