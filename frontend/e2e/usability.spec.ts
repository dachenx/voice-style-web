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

test('创建响应丢失后关闭重开仍重放原请求，同名不同请求仍可建', async ({ page }) => {
  const name = `幂等-${randomUUID()}`; const bodies: string[] = []; let lost = true;
  await page.route('**/api/projects', async route => {
    if (route.request().method() === 'POST') {
      bodies.push(route.request().postData()!);
      if (lost) { lost = false; await route.fetch(); await route.abort(); return; }
    }
    await route.continue();
  });
  await page.goto('/'); await page.getByRole('button', { name: '新建项目', exact: true }).click();
  await page.getByLabel('项目名称', { exact: true }).fill(name);
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('创建结果请核对');
  await expect(page.getByLabel('项目名称', { exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await page.getByRole('button', { name: '新建项目', exact: true }).click();
  await expect(page.getByLabel('项目名称', { exact: true })).toHaveValue(name);
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.getByRole('region', { name: '项目详情' })).toContainText(name);
  expect(bodies[0]).toBe(bodies[1]);
  const projects = (await (await page.request.get('/api/projects')).json()).projects;
  expect(projects.filter((p: { name: string }) => p.name === name)).toHaveLength(1);
});

test('归档拒绝未保存稿，归档恢复保留内容，失败不假报成功', async ({ page }) => {
  const id = await ready(page);
  await page.getByLabel('武汉话草稿', { exact: true }).first().fill('归档前未保存稿');
  await page.getByRole('button', { name: '归档项目', exact: true }).click();
  await expect(page.getByRole('status', { name: '项目操作状态' })).toContainText('先保存');
  await page.getByRole('button', { name: '保存文案', exact: true }).click();
  await expect(page.getByRole('status', { name: '文案操作状态' })).toContainText('已保存');
  let fail = true;
  await page.route(`**/api/projects/${id}/archive`, async route => {
    if (fail) { fail = false; await route.abort(); } else await route.continue();
  });
  page.on('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '归档项目', exact: true }).click();
  await expect(page.getByRole('status', { name: '项目操作状态' })).toContainText('需要核对');
  await page.getByRole('button', { name: '归档项目', exact: true }).click();
  await expect(page.getByRole('region', { name: '项目详情' })).toHaveCount(0);
  await expect(page.locator(`a[href="#project=${id}"]`)).toHaveCount(0);
  await page.getByRole('button', { name: '已归档', exact: true }).click();
  await page.locator(`a[href="#project=${id}"]`).click();
  await expect(page.getByLabel('武汉话草稿', { exact: true }).first()).toHaveValue('归档前未保存稿');
  await page.getByRole('button', { name: '恢复项目', exact: true }).click();
  await page.getByRole('button', { name: '活跃项目', exact: true }).click();
  await page.locator(`a[href="#project=${id}"]`).click();
  await expect(page.locator('article[data-task-id]')).toHaveCount(1);
  await expect(page.getByLabel('武汉话草稿', { exact: true }).first()).toHaveValue('归档前未保存稿');
});

test('复制取消和撤销只恢复草稿；误删恢复原UUID顺序且保留其他编辑', async ({ page }) => {
  await ready(page);
  const first = page.locator('[data-paragraph-id]').first();
  const id = await first.getAttribute('data-paragraph-id');
  const oldDraft = await first.getByLabel('武汉话草稿', { exact: true }).inputValue();
  page.once('dialog', d => d.dismiss());
  await first.getByRole('button', { name: '复制原稿到草稿' }).click();
  await expect(first.getByLabel('武汉话草稿', { exact: true })).toHaveValue(oldDraft);
  page.once('dialog', d => d.accept());
  await first.getByRole('button', { name: '复制原稿到草稿' }).click();
  await first.getByLabel('普通话原稿', { exact: true }).fill('复制后新原稿');
  await first.getByLabel('读音备注').fill('复制后新备注');
  await page.getByRole('button', { name: '撤销最近一次复制或删除' }).click();
  await expect(first.getByLabel('武汉话草稿', { exact: true })).toHaveValue(oldDraft);
  await expect(first.getByLabel('普通话原稿', { exact: true })).toHaveValue('复制后新原稿');
  await expect(first.getByLabel('读音备注')).toHaveValue('复制后新备注');
  await first.getByRole('button', { name: '删除段落' }).click();
  await page.getByLabel('武汉话草稿', { exact: true }).first().fill('其他段修改保留');
  await page.getByRole('button', { name: '撤销最近一次复制或删除' }).click();
  await expect(first).toHaveAttribute('data-paragraph-id', id!);
  await expect(page.getByLabel('武汉话草稿', { exact: true }).nth(1)).toHaveValue('其他段修改保留');
  await expect(first.getByLabel('读音备注')).toHaveValue('复制后新备注');
});

test('初始文案失败明确停止加载并允许重试', async ({ page }) => {
  const id = (await (await page.request.post('/api/projects', { data: { name: '读取失败' } })).json()).id;
  let fail = true;
  await page.route(`**/api/projects/${id}/text`, async route => {
    if (fail) { fail = false; await route.fulfill({ status: 503, json: { error: { message: '注入读取失败' } } }); }
    else await route.continue();
  });
  await page.goto(`/#project=${id}`);
  await expect(page.getByRole('region', { name: '文案工作台' })).toContainText('文案未能读取');
  await expect(page.getByText('正在读取文案…', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '重新载入服务器版本' }).click();
  await expect(page.getByRole('button', { name: '添加段落' })).toBeVisible();
});

test('历史未匹配时导出未知结果禁新请求，刷新及切项目保留设置和原请求', async ({ page }) => {
  const id = await ready(page); const bodies: string[] = []; let lost = true;
  await panel(page).getByRole('spinbutton').fill('500');
  await page.getByRole('button', { name: '刷新参考与组件' }).click();
  await expect(panel(page).getByRole('spinbutton')).toHaveValue('500');
  await page.route(`**/api/projects/${id}/exports`, async route => {
    if (route.request().method() === 'POST') {
      bodies.push(route.request().postData()!);
      if (lost) { lost = false; await route.fetch(); await route.abort(); return; }
    } else if (route.request().method() === 'GET') {
      await route.fulfill({ json: [] }); return; // Unmatched history must preserve explicit retry.
    }
    await route.continue();
  });
  await panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toContainText('需要核对');
  await expect(panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '刷新参考与组件' }).click();
  await expect(panel(page).getByRole('button', { name: '重试同一导出请求' })).toBeEnabled();
  const other = (await (await page.request.post('/api/projects', { data: { name: '隔离另一项目' } })).json()).id;
  await page.getByRole('button', { name: '刷新项目列表' }).click();
  await page.locator(`a[href="#project=${other}"]`).click();
  await expect(panel(page).getByRole('spinbutton')).toHaveValue('0');
  await expect(panel(page).getByRole('button', { name: '重试同一导出请求' })).toHaveCount(0);
  await page.locator(`a[href="#project=${id}"]`).click();
  await expect(panel(page).getByRole('spinbutton')).toHaveValue('500');
  await panel(page).getByRole('button', { name: '重试同一导出请求' }).click();
  await expect(panel(page).getByRole('status')).toContainText('导出已保存');
  expect(bodies).toHaveLength(2); expect(bodies[0]).toBe(bodies[1]);
  expect(await (await page.request.get(`/api/projects/${id}/exports`)).json()).toHaveLength(1);
});

test('确定拒绝可修正新导出，冲突或未知结果仍保留原请求', async ({ page }) => {
  const id = await ready(page); let code = 'export_inputs_changed';
  await page.route(`**/api/projects/${id}/exports`, async route => {
    if (route.request().method() === 'POST') await route.fulfill({ status: 409, json: { error: { code, message: '注入冲突' } } });
    else await route.continue();
  });
  await panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toContainText('导出未创建');
  await expect(panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true })).toBeEnabled();
  code = 'export_conflict';
  await panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toContainText('需要核对');
  await expect(panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true })).toBeDisabled();
  await expect(panel(page).getByRole('button', { name: '重试同一导出请求' })).toBeEnabled();
});

test('隐藏的未保存参考原话也阻止该项目归档', async ({ page }) => {
  await ready(page);
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('cosyvoice3-local');
  const text = page.getByLabel('参考片段准确原话', { exact: true });
  await expect(text).toBeEnabled(); await text.fill('还没有保存的测试原话');
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('simulated-tone');
  await page.getByRole('button', { name: '归档项目', exact: true }).click();
  await expect(page.getByRole('status', { name: '项目操作状态' })).toContainText('先保存');
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('cosyvoice3-local');
  await expect(text).toHaveValue('还没有保存的测试原话');
});


test('在途导出重挂后仍显示确定拒绝，归档加载失败不混入活跃列表', async ({ page }) => {
  const id = await ready(page);
  let release!: () => void;
  const barrier = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/projects/${id}/exports`, async route => {
    if (route.request().method() !== 'POST') { await route.continue(); return; }
    await barrier;
    await route.fulfill({ status: 409, json: { error: { code: 'export_inputs_changed', message: '输入已变化' } } });
  });
  await panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true }).click();
  await page.getByRole('button', { name: '刷新参考与组件' }).click();
  await expect(panel(page).getByRole('button', { name: '重试同一导出请求' })).toBeDisabled();
  release();
  await expect(panel(page).getByRole('alert')).toContainText('导出未创建');
  await expect(panel(page).getByRole('button', { name: '导出整篇 WAV', exact: true })).toBeEnabled();
  await page.route('**/api/projects?archived=true', route => route.fulfill({ status: 503, json: { error: { message: '分类读取失败' } } }));
  await page.getByRole('button', { name: '已归档', exact: true }).click();
  await expect(page.getByText('分类读取失败', { exact: true })).toBeVisible();
  await expect(page.locator(`a[href="#project=${id}"]`)).toHaveCount(0);
});
