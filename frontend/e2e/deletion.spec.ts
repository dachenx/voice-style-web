import { expect, test } from './advancedFixture';
import { randomUUID } from 'node:crypto';

test('永久删除两次确认，取消无写且无需输入名称，成功只删除目标', async ({ page }) => {
  const name = `删除-${randomUUID()}`;
  const p = await (await page.request.post('/api/projects', { data: { name } })).json();
  const other = await (await page.request.post('/api/projects', { data: { name: '保留项目' } })).json();
  let deletes = 0;
  await page.route(`**/api/projects/${p.id}`, async route => { if (route.request().method() === 'DELETE') deletes++; await route.continue(); });
  await page.goto(`/#project=${p.id}`);
  await page.getByRole('button', { name: '永久删除项目', exact: true }).click();
  const modal = page.getByRole('dialog', { name: '永久删除项目' });
  await expect(modal).toContainText('不能恢复');
  await modal.getByRole('button', { name: '取消', exact: true }).click();
  expect(deletes).toBe(0);
  await page.getByRole('button', { name: '永久删除项目', exact: true }).click();
  await expect(modal.getByRole('textbox')).toHaveCount(0);
  await expect(modal.getByRole('button', { name: '确认永久删除' })).toBeEnabled();
  await modal.getByRole('button', { name: '确认永久删除' }).click();
  await expect(page.getByRole('status', { name: '项目操作状态' })).toContainText('已永久删除');
  await expect(page.locator(`a[href="#project=${p.id}"]`)).toHaveCount(0);
  expect((await page.request.get(`/api/projects/${p.id}`)).status()).toBe(410);
  expect((await page.request.get(`/api/projects/${other.id}`)).ok()).toBeTruthy();
  await page.goto(`/#project=${p.id}`);
  await expect(page.getByRole('region', { name: '项目详情' })).toContainText('已永久删除');
  await page.getByRole('button', { name: '核对删除清理状态' }).click();
  await page.getByRole('button', { name: '重试同一项目删除' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(deletes).toBe(1);
});

test('删除响应丢失可关闭、刷新和继续核对，不重复删除或影响同名项目', async ({ page }) => {
  const p = await (await page.request.post('/api/projects', { data: { name: '同名项目' } })).json();
  const other = await (await page.request.post('/api/projects', { data: { name: '同名项目' } })).json();
  let deletes = 0;
  await page.route(`**/api/projects/${p.id}`, async route => {
    if (route.request().method() === 'DELETE') { deletes++; await route.fetch(); await route.abort(); }
    else await route.continue();
  });
  await page.goto(`/#project=${p.id}`);
  await page.getByRole('button', { name: '永久删除项目', exact: true }).click();
  await page.getByRole('button', { name: '确认永久删除' }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('待核对');
  await page.getByRole('button', { name: '稍后核对' }).click();
  await page.reload();
  await page.getByRole('button', { name: '继续核对删除' }).click();
  await page.getByRole('button', { name: '重试同一项目删除' }).click();
  await expect(page.getByRole('status', { name: '项目操作状态' })).toContainText('已永久删除');
  expect(deletes).toBe(1);
  expect((await page.request.get(`/api/projects/${other.id}`)).ok()).toBeTruthy();
  await expect(page.getByRole('button', { name: '继续核对删除' })).toHaveCount(0);
});

test('归档项目同样可删，未保存稿先保护，任务拒绝不得假报成功', async ({ page }) => {
  const p = await (await page.request.post('/api/projects', { data: { name: '归档删除' } })).json();
  await page.request.put(`/api/projects/${p.id}/archive`, { data: { archived: true } });
  await page.goto(`/#project=${p.id}`);
  await page.getByRole('button', { name: '添加段落' }).click();
  await page.getByLabel('普通话原稿', { exact: true }).fill('未保存');
  await page.getByRole('button', { name: '永久删除项目', exact: true }).click();
  await expect(page.getByRole('status', { name: '项目操作状态' })).toContainText('先保存');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  page.once('dialog', d => d.accept());
  await page.getByRole('button', { name: '重新载入服务器版本' }).click();
  await expect(page.getByLabel('普通话原稿', { exact: true })).toHaveCount(0);
  let blocked = true;
  await page.route(`**/api/projects/${p.id}`, async route => {
    if (route.request().method() === 'DELETE' && blocked) await route.fulfill({ status: 409, json: { error: { code: 'project_tasks_active', message: '项目有排队或运行任务' } } });
    else await route.continue();
  });
  await page.getByRole('button', { name: '永久删除项目', exact: true }).click();
  await page.getByRole('button', { name: '确认永久删除' }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('运行任务');
  expect((await page.request.get(`/api/projects/${p.id}`)).ok()).toBeTruthy();
  blocked = false;
  await page.getByRole('button', { name: '确认永久删除' }).click();
  await expect(page.getByRole('status', { name: '项目操作状态' })).toContainText('已永久删除');
});

test('创建响应丢失后项目被删，旧请求终态释放且新建不复活旧ID', async ({ page }) => {
  let lost = true; let oldId = '';
  await page.route('**/api/projects', async route => {
    if (route.request().method() === 'POST' && lost) {
      lost = false; const response = await route.fetch(); oldId = (await response.json()).id; await route.abort();
    } else await route.continue();
  });
  await page.goto('/');
  await page.getByRole('button', { name: '新建项目', exact: true }).click();
  await page.getByLabel('项目名称', { exact: true }).fill('请求已删除');
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('创建结果请核对');
  expect((await page.request.delete(`/api/projects/${oldId}`, { data: { confirm_name: '请求已删除', acknowledged: true } })).ok()).toBeTruthy();
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('旧项目不会恢复');
  await expect(page.getByLabel('项目名称', { exact: true })).toBeEnabled();
  await page.getByLabel('项目名称', { exact: true }).fill('全新请求');
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByRole('region', { name: '项目详情' })).toContainText('全新请求');
  expect((await page.request.get(`/api/projects/${oldId}`)).status()).toBe(410);
});

test('未决删除不被其他项目覆盖，状态核对忙也保留入口', async ({ page }) => {
  const a = await (await page.request.post('/api/projects', { data: { name: 'A待清理' } })).json();
  const b = await (await page.request.post('/api/projects', { data: { name: 'B保留' } })).json();
  let busy = true;
  await page.route(`**/api/projects/${a.id}`, async route => {
    if (route.request().method() === 'DELETE') { await route.fetch(); await route.fulfill({ status: 503, json: { error: { code: 'delete_cleanup_pending', message: '项目已移除，但文件清理未完成' } } }); }
    else await route.continue();
  });
  await page.route(`**/api/projects/${a.id}/deletion-status`, async route => {
    if (busy) await route.fulfill({ status: 409, json: { error: { code: 'project_busy', message: '仍在清理' } } });
    else await route.continue();
  });
  await page.goto(`/#project=${a.id}`);
  await page.getByRole('button', { name: '永久删除项目', exact: true }).click();
  await page.getByRole('button', { name: '确认永久删除' }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('清理未完成');
  await page.getByRole('button', { name: '稍后核对' }).click();
  await page.locator(`a[href="#project=${b.id}"]`).click();
  await page.getByRole('button', { name: '永久删除项目', exact: true }).click();
  await expect(page.getByRole('status', { name: '项目操作状态' })).toContainText('先核对上一次');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: '继续核对删除' }).click();
  await page.getByRole('button', { name: '重试同一项目删除' }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('待核对');
  await page.getByRole('button', { name: '稍后核对' }).click();
  await expect(page.getByRole('button', { name: '继续核对删除' })).toBeVisible();
  busy = false;
  await page.getByRole('button', { name: '继续核对删除' }).click();
  await page.getByRole('button', { name: '重试同一项目删除' }).click();
  await expect(page.getByRole('button', { name: '继续核对删除' })).toHaveCount(0);
  expect((await page.request.get(`/api/projects/${b.id}`)).ok()).toBeTruthy();
});

test('核对确认已删但清理重试忙，仍保留跨刷新恢复入口', async ({ page }) => {
  const p = await (await page.request.post('/api/projects', { data: { name: '清理重试忙' } })).json();
  let attempt = 0; let complete = false;
  await page.route(`**/api/projects/${p.id}`, async route => {
    if (route.request().method() !== 'DELETE') { await route.continue(); return; }
    attempt++;
    if (attempt === 1) { await route.fetch(); await route.abort(); }
    else await route.fulfill({ status: 409, json: { error: { code: 'project_busy', message: '另一个窗口仍在清理' } } });
  });
  await page.route(`**/api/projects/${p.id}/deletion-status`, route => route.fulfill({ json: { deleted: true, cleanup_complete: complete } }));
  await page.goto(`/#project=${p.id}`);
  await page.getByRole('button', { name: '永久删除项目', exact: true }).click();
  await page.getByRole('button', { name: '确认永久删除' }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('待核对');
  await page.getByRole('button', { name: '重试同一项目删除' }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('仍在清理');
  await page.getByRole('button', { name: '稍后核对' }).click();
  await page.reload();
  await page.getByRole('button', { name: '继续核对删除' }).click();
  complete = true;
  await page.getByRole('button', { name: '重试同一项目删除' }).click();
  await expect(page.getByRole('button', { name: '继续核对删除' })).toHaveCount(0);
});

test('首次删除未到服务器后新增草稿，继续核对必须重新保护未保存稿', async ({ page }) => {
  const p = await (await page.request.post('/api/projects', { data: { name: '保留新稿' } })).json();
  let deletes = 0;
  await page.route(`**/api/projects/${p.id}`, async route => {
    if (route.request().method() === 'DELETE') { deletes++; await route.abort(); }
    else await route.continue();
  });
  await page.goto(`/#project=${p.id}`);
  await page.getByRole('button', { name: '永久删除项目', exact: true }).click();
  await page.getByRole('button', { name: '确认永久删除' }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('待核对');
  await page.getByRole('button', { name: '稍后核对' }).click();
  await page.getByRole('button', { name: '添加段落' }).click();
  await page.getByLabel('普通话原稿', { exact: true }).fill('新增未保存稿');
  await page.getByRole('button', { name: '继续核对删除' }).click();
  await page.getByRole('button', { name: '重试同一项目删除' }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('有未保存');
  expect(deletes).toBe(1);
  await page.getByRole('button', { name: '稍后核对' }).click();
  await expect(page.getByLabel('普通话原稿', { exact: true })).toHaveValue('新增未保存稿');
  expect((await page.request.get(`/api/projects/${p.id}`)).ok()).toBeTruthy();
});
