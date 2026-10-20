import { expect, test, type Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';

async function setup(page: Page) {
  const project = await (await page.request.post('/api/projects', { data: { name: '文案导入' } })).json();
  const paragraphs = [1, 2].map(n => ({ id: randomUUID(), original_text: `原稿${n}`, draft_text: `旧文案${n}`, pronunciation_note: '' }));
  await page.request.put(`/api/projects/${project.id}/text`, { data: { expected_revision: 0, source_language: 'zh-CN', target_language: 'wuhan', register: '', terms: '', paragraphs } });
  await page.goto(`/#project=${project.id}`);
  await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('旧文案1');
  return { project, paragraphs };
}
const file = (page: Page, text: string, name = '稿件.txt') => page.getByLabel('导入 TXT / Word 文案').setInputFiles({ name, mimeType: 'application/octet-stream', buffer: Buffer.from(text) });

test('文件预览取消与覆盖确认，保留段落身份、其他段和服务器旧稿', async ({ page }) => {
  const { project, paragraphs } = await setup(page);
  await file(page, '导入内容\r\n第二行');
  await expect(page.getByLabel('导入文字预览')).toHaveValue('导入内容\n第二行');
  await page.getByRole('button', { name: '取消导入' }).click();
  await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('旧文案1');
  await file(page, '导入内容');
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('button', { name: '确认替换当前段文案' }).click();
  await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('旧文案1');
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '确认替换当前段文案' }).click();
  await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('导入内容');
  expect((await (await page.request.get(`/api/projects/${project.id}/text`)).json()).paragraphs).toEqual(paragraphs);
  await page.getByLabel('编辑哪一段').selectOption(paragraphs[1].id);
  await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('旧文案2');
  await page.getByRole('button', { name: '保存并确认文案与顺序', exact: true }).click();
  await expect(page.getByLabel('文案操作状态')).toContainText('已保存并确认');
  const result = (await (await page.request.get(`/api/projects/${project.id}/text`)).json()).paragraphs;
  expect(result).toEqual([{ ...paragraphs[0], draft_text: '导入内容' }, paragraphs[1]]);
});

test('坏文件、旧doc、空白及超限文件不覆盖；预览编辑仍校验上限', async ({ page }) => {
  await setup(page);
  for (const [name, text] of [['坏.docx', 'bad'], ['旧.doc', 'old'], ['空.txt', ' '], ['长.txt', '字'.repeat(5001)]]) {
    await file(page, text, name); await expect(page.getByRole('region', { name: '导入文案', exact: true }).getByRole('alert')).toBeVisible();
    await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('旧文案1');
  }
  await file(page, '短句'); await page.getByLabel('导入文字预览').fill('字'.repeat(5001));
  await page.getByRole('button', { name: '确认替换当前段文案' }).click();
  await expect(page.getByRole('region', { name: '导入文案', exact: true })).toContainText('最多 5000');
  await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('旧文案1');
});

test('解析期间改稿和切段不被迟到结果覆盖', async ({ page }) => {
  const { paragraphs } = await setup(page);
  let release!: () => void; const barrier = new Promise<void>(resolve => { release = resolve; });
  let entered!: () => void; const start = new Promise<void>(resolve => { entered = resolve; });
  await page.route('**/api/text-import?*', async route => { const response = await route.fetch(); entered(); await barrier; await route.fulfill({ response }); });
  await file(page, '迟到导入'); await start;
  await page.getByLabel('要朗读的文案', { exact: true }).fill('读取期间新修改');
  release();
  await expect(page.getByLabel('导入文字预览')).toHaveValue('迟到导入');
  await expect(page.getByRole('button', { name: '确认替换当前段文案' })).toBeDisabled();
  await page.getByLabel('编辑哪一段').selectOption(paragraphs[1].id);
  await expect(page.getByLabel('导入文字预览')).toHaveCount(0);
  await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('旧文案2');
});

test('DOCX extracts real body text', async ({ page }) => {
  await setup(page);
  await page.getByLabel('导入 TXT / Word 文案').setInputFiles({ name: 'sample.docx', mimeType: 'application/octet-stream', buffer: Buffer.from('UEsDBBQAAAAAALhUVF1w7rBCnwAAAJ8AAAARAAAAd29yZC9kb2N1bWVudC54bWw8dzpkb2N1bWVudCB4bWxuczp3PSJodHRwOi8vc2NoZW1hcy5vcGVueG1sZm9ybWF0cy5vcmcvd29yZHByb2Nlc3NpbmdtbC8yMDA2L21haW4iPjx3OmJvZHk+PHc6cD48dzpyPjx3OnQ+V29yZCB0ZXh0IDEyODwvdzp0PjwvdzpyPjwvdzpwPjwvdzpib2R5Pjwvdzpkb2N1bWVudD5QSwECFAMUAAAAAAC4VFRdcO6wQp8AAACfAAAAEQAAAAAAAAAAAAAAgAEAAAAAd29yZC9kb2N1bWVudC54bWxQSwUGAAAAAAEAAQA/AAAAzgAAAAAA', 'base64') });
  await expect(page.getByLabel('导入文字预览')).toHaveValue('Word text 128');
});

for (const target of ['段落', '项目']) {
  test(`读取在途切换${target}不出现旧预览`, async ({ page }) => {
    const { paragraphs } = await setup(page);
    const other = await (await page.request.post('/api/projects', { data: { name: '另一个导入项目' } })).json();
    let release!: () => void; const barrier = new Promise<void>(resolve => { release = resolve; });
    let entered!: () => void; const start = new Promise<void>(resolve => { entered = resolve; });
    let handled!: () => void; const done = new Promise<void>(resolve => { handled = resolve; });
    const terminated = page.waitForEvent('requestfailed', request => request.url().includes('/api/text-import?'));
    await page.route('**/api/text-import?*', async route => {
      try { const response = await route.fetch(); entered(); await barrier; await route.fulfill({ response }); }
      catch { /* Switching targets aborts this request. Assert that event below. */ }
      finally { handled(); }
    });
    await file(page, '不能串入'); await start;
    if (target === '段落') await page.getByLabel('编辑哪一段').selectOption(paragraphs[1].id);
    else await page.goto(`/#project=${other.id}`);
    release();
    await done;
    await terminated;
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue(target === '段落' ? '旧文案2' : '');
    await expect(page.getByLabel('导入文字预览')).toHaveCount(0);
  });
}

test('替换导入遵守整篇20000字符上限', async ({ page }) => {
  const { project } = await setup(page);
  const paragraphs = Array.from({ length: 5 }, () => ({ id: randomUUID(), original_text: '原稿', draft_text: '字'.repeat(4000), pronunciation_note: '' }));
  expect((await page.request.put(`/api/projects/${project.id}/text`, { data: { expected_revision: 1, source_language: 'zh-CN', target_language: 'wuhan', register: '', terms: '', paragraphs } })).ok()).toBe(true);
  await page.reload();
  await file(page, '新'.repeat(4001));
  await page.getByRole('button', { name: '确认替换当前段文案' }).click();
  await expect(page.getByRole('region', { name: '导入文案', exact: true })).toContainText('整篇原稿和草稿各最多 20000 字');
  await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('字'.repeat(4000));
});
