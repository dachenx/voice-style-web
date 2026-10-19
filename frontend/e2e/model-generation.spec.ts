import { expect, test, type Page } from './advancedFixture';
import { randomUUID } from 'node:crypto';
import type { Project } from '../src/api';
import type { ReferenceTranscript } from '../src/ReferenceTranscript';
import type { TextDocument } from '../src/TextPanel';

type Seed = { project: Project; referenceId: string; clipId: string; transcript: ReferenceTranscript };
const editor = (page: Page) => page.getByRole('region', { name: '参考片段原话', exact: true });
const words = (page: Page) => editor(page).getByLabel('参考片段准确原话', { exact: true });
const save = (page: Page) => editor(page).getByRole('button', { name: '保存片段原话', exact: true });
const reload = (page: Page) => editor(page).getByRole('button', { name: '重新载入片段原话', exact: true });
const realPanel = (page: Page) => page.getByRole('region', { name: '真实语音与任务', exact: true });
const transcriptURL = (seed: Seed) => `/api/projects/${seed.project.id}/references/${seed.referenceId}/clips/${seed.clipId}/transcript`;

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

// Synthetic reference bytes and artificial text only; these do not test speech quality.
function tone(): Buffer {
  const frames = 24000;
  const bytes = Buffer.alloc(44 + frames * 2);
  bytes.write('RIFF', 0); bytes.writeUInt32LE(bytes.length - 8, 4); bytes.write('WAVEfmt ', 8);
  bytes.writeUInt32LE(16, 16); bytes.writeUInt16LE(1, 20); bytes.writeUInt16LE(1, 22);
  bytes.writeUInt32LE(24000, 24); bytes.writeUInt32LE(48000, 28);
  bytes.writeUInt16LE(2, 32); bytes.writeUInt16LE(16, 34);
  bytes.write('data', 36); bytes.writeUInt32LE(frames * 2, 40);
  for (let index = 0; index < frames; index += 1) {
    bytes.writeInt16LE(Math.round(4000 * Math.sin(2 * Math.PI * 400 * index / 24000)), 44 + index * 2);
  }
  return bytes;
}

async function readTranscript(page: Page, seed: Seed) {
  const response = await page.request.get(transcriptURL(seed));
  expect(response.status()).toBe(200);
  return await response.json() as ReferenceTranscript;
}

async function writeTranscript(page: Page, seed: Seed, text: string) {
  const previous = await readTranscript(page, seed);
  const response = await page.request.put(transcriptURL(seed), { data: {
    expected_revision: previous.revision, expected_clip_sha256: previous.clip_sha256, text,
  } });
  expect(response.status()).toBe(200);
  return await response.json() as ReferenceTranscript;
}

async function seed(page: Page, name: string): Promise<Seed> {
  const created = await page.request.post('/api/projects', { data: { name } });
  expect(created.status()).toBe(201);
  const project = await created.json() as Project;
  const saved = await page.request.put(`/api/projects/${project.id}/text`, { data: {
    expected_revision: 0, source_language: 'zh-CN', target_language: 'wuhan', register: '自然口语', terms: '',
    paragraphs: [{ id: randomUUID(), original_text: '人工流程原稿，金额 123 元。',
      draft_text: '人工流程确认草稿，金额 123 元。', pronunciation_note: '这里只测试软件流程' }],
  } });
  expect(saved.status()).toBe(200);
  const document = await saved.json() as TextDocument;
  const confirmed = await page.request.post(`/api/projects/${project.id}/text/confirm`, {
    data: { expected_revision: document.revision, acknowledged: true },
  });
  expect(confirmed.status()).toBe(200);
  const uploaded = await page.request.post(`/api/projects/${project.id}/references?filename=synthetic-transcript.wav`, {
    data: tone(), headers: { 'Content-Type': 'application/octet-stream' },
  });
  expect(uploaded.status()).toBe(201);
  const referenceId = (await uploaded.json() as { id: string }).id;
  const clipped = await page.request.post(`/api/projects/${project.id}/references/${referenceId}/clips`, {
    data: { start_ms: 0, end_ms: 700 },
  });
  expect(clipped.status()).toBe(201);
  const clipId = (await clipped.json() as { id: string }).id;
  const value = { project, referenceId, clipId } as Seed;
  value.transcript = await writeTranscript(page, value, '片段甲的人工测试原话，金额 123 元。');
  return value;
}

async function open(page: Page, seed: Seed) {
  await page.goto(`/#project=${seed.project.id}`);
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('cosyvoice3-local');
  await expect(words(page)).toBeEnabled();
  await expect(words(page)).toHaveValue(seed.transcript.text);
}

async function saveWords(page: Page, seed: Seed, expectedStatus = 200) {
  const event = page.waitForResponse((response) => new URL(response.url()).pathname === transcriptURL(seed)
    && response.request().method() === 'PUT');
  await save(page).click();
  const response = await event;
  expect(response.status()).toBe(expectedStatus);
  return response;
}

async function rendered(page: Page) {
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
}

async function answerReload(page: Page, accept: boolean) {
  const handled = new Promise<void>((resolve, reject) => {
    page.once('dialog', async (dialog) => {
      try {
        expect(dialog.type()).toBe('confirm');
        expect(dialog.message()).toContain('丢弃尚未保存的片段原话');
        if (accept) await dialog.accept(); else await dialog.dismiss();
        resolve();
      } catch (cause) { reject(cause); }
    });
  });
  await reload(page).click();
  await handled;
}

async function chooseProject(page: Page, value: Seed) {
  await page.getByRole('region', { name: '项目列表' }).locator(`a[href="#project=${value.project.id}"]`).click();
  await expect(page.getByRole('region', { name: '项目详情', exact: true }).getByRole('heading', { name: value.project.name, exact: true })).toBeVisible();
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('cosyvoice3-local');
  await expect(words(page)).toBeEnabled();
}

async function chooseClip(page: Page, clipId: string) {
  await page.getByLabel('选择试听片段', { exact: true }).selectOption(clipId);
  await page.getByRole('button', { name: '设为当前参考', exact: true }).click();
  await expect(words(page)).toHaveAttribute('id', `transcript-${clipId}`);
  await expect(words(page)).toBeEnabled();
}

async function returnFromSimulation(page: Page) {
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('simulated-tone');
  await expect(editor(page)).toHaveCount(0);
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('cosyvoice3-local');
  await expect(words(page)).toBeEnabled();
}

test('原话保存同步真实项目时间排序并可刷新恢复，未保存和空原话不阻断模拟', async ({ page }) => {
  const value = await seed(page, '原话保存时间排序');
  const newerResponse = await page.request.post('/api/projects', { data: { name: '原话保存前的对照项目' } });
  expect(newerResponse.status()).toBe(201);
  const newer = await newerResponse.json() as Project;
  const before = await (await page.request.get(`/api/projects/${value.project.id}`)).json() as Project;
  await open(page, value);
  const list = page.getByRole('region', { name: '项目列表', exact: true });
  await expect(list.getByRole('link').first()).toHaveAttribute('href', `#project=${newer.id}`);
  const changedText = '原话经人工修改，金额 456 元。';
  await words(page).fill(changedText);
  await expect(editor(page)).toContainText('片段原话尚未保存');
  await expect(realPanel(page).getByRole('button', { name: '提交真实短段任务', exact: true })).toBeDisabled();
  const response = await saveWords(page, value);
  const saved = await response.json() as ReferenceTranscript;
  expect(saved.revision).toBe(value.transcript.revision + 1);
  expect(saved.clip_id).toBe(value.clipId);
  expect(saved.clip_sha256).toBe(value.transcript.clip_sha256);
  expect(saved.text).toBe(changedText);
  await expect(page.getByRole('status', { name: '片段原话状态' })).toContainText(`版本 ${saved.revision}`);
  const updated = await (await page.request.get(`/api/projects/${value.project.id}`)).json() as Project;
  expect(updated.updated_at).not.toBe(before.updated_at);
  await expect(list.getByRole('link').first()).toHaveAttribute('href', `#project=${value.project.id}`);
  await expect(list.locator(`a[href="#project=${value.project.id}"] time`)).toHaveAttribute('datetime', updated.updated_at);
  await expect(page.getByRole('region', { name: '项目详情' }).getByText('最近更新', { exact: true }).locator('..').locator('time'))
    .toHaveAttribute('datetime', updated.updated_at);
  await page.reload();
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('cosyvoice3-local');
  await expect(words(page)).toHaveValue(changedText);
  await words(page).fill('未保存的参考原话');
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('simulated-tone');
  await expect(page.getByRole('button', { name: '提交模拟全文任务', exact: true })).toBeEnabled();
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('cosyvoice3-local');
  await expect(words(page)).toHaveValue('未保存的参考原话');
  await words(page).fill('');
  await saveWords(page, value);
  expect((await readTranscript(page, value)).text).toBe('');
  await expect(realPanel(page).getByRole('button', { name: '提交真实全文任务', exact: true })).toBeDisabled();
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('simulated-tone');
  await expect(page.getByRole('button', { name: '提交模拟全文任务', exact: true })).toBeEnabled();
});

test('原话草稿在组件片段和项目往返后分别恢复，未保存内容不写入服务器', async ({ page }) => {
  const first = await seed(page, '保留原话的项目甲');
  const second = await seed(page, '保留原话的项目乙');
  const response = await page.request.post(`/api/projects/${first.project.id}/references/${first.referenceId}/clips`, {
    data: { start_ms: 200, end_ms: 900 },
  });
  expect(response.status()).toBe(201);
  const sibling = { ...first, clipId: (await response.json() as { id: string }).id };
  sibling.transcript = await writeTranscript(page, sibling, '项目甲另一片段的服务端原话');
  const activate = await page.request.put(`/api/projects/${first.project.id}/active-clip`, { data: { clip_id: first.clipId } });
  expect(activate.status()).toBe(200);
  await open(page, first);
  await words(page).fill('项目甲首片段的本地草稿');
  await returnFromSimulation(page);
  await expect(words(page)).toHaveValue('项目甲首片段的本地草稿');
  await expect(save(page)).toBeEnabled();
  await chooseClip(page, sibling.clipId);
  await expect(words(page)).toHaveValue(sibling.transcript.text);
  await words(page).fill('项目甲另一片段的本地草稿');
  await chooseClip(page, first.clipId);
  await expect(words(page)).toHaveValue('项目甲首片段的本地草稿');
  await chooseProject(page, second);
  await expect(words(page)).toHaveValue(second.transcript.text);
  await words(page).fill('项目乙的独立本地草稿');
  await chooseProject(page, first);
  await expect(words(page)).toHaveValue('项目甲首片段的本地草稿');
  await chooseClip(page, sibling.clipId);
  await expect(words(page)).toHaveValue('项目甲另一片段的本地草稿');
  await chooseProject(page, second);
  await expect(words(page)).toHaveValue('项目乙的独立本地草稿');
  await expect(save(page)).toBeEnabled();
  for (const value of [first, sibling, second]) expect((await readTranscript(page, value)).text).toBe(value.transcript.text);
});

test('恢复原话保留旧保存基线，服务器新版触发真实冲突，只有明确重载成功才清除草稿', async ({ page }) => {
  const value = await seed(page, '原话草稿保存基线');
  await open(page, value);
  const localText = '切换组件也不能丢失的本地原话';
  await words(page).fill(localText);
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('simulated-tone');
  const remote = await writeTranscript(page, value, '另一个窗口已保存的新版原话');
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('cosyvoice3-local');
  await expect(words(page)).toBeEnabled();
  await expect(words(page)).toHaveValue(localText);
  const failed = await saveWords(page, value, 409);
  expect(failed.request().postDataJSON()).toEqual({
    expected_revision: value.transcript.revision, expected_clip_sha256: value.transcript.clip_sha256, text: localText,
  });
  await expect(editor(page).getByRole('alert')).toContainText('参考原话已更新');
  await expect(words(page)).toHaveValue(localText);
  expect((await readTranscript(page, value)).text).toBe(remote.text);
  await answerReload(page, false);
  await returnFromSimulation(page);
  await expect(words(page)).toHaveValue(localText);
  await page.route(`**${transcriptURL(value)}`, (route) => route.fulfill({
    status: 503, contentType: 'application/json', body: JSON.stringify({
      error: { code: 'storage_unavailable', message: '测试注入：明确重载仍失败' },
    }),
  }), { times: 1 });
  await answerReload(page, true);
  await expect(editor(page).getByRole('alert')).toContainText('测试注入：明确重载仍失败');
  await expect(words(page)).toHaveValue(localText);
  await returnFromSimulation(page);
  await expect(words(page)).toHaveValue(localText);
  await saveWords(page, value, 409);
  await expect(words(page)).toHaveValue(localText);
  await answerReload(page, true);
  await expect(words(page)).toHaveValue(remote.text);
  await expect(words(page)).toBeEnabled();
  await expect(save(page)).toBeDisabled();
  await expect(editor(page).getByRole('alert')).toHaveCount(0);
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('simulated-tone');
  const newest = await writeTranscript(page, value, '清除缓存后应读取的更新原话');
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('cosyvoice3-local');
  await expect(words(page)).toHaveValue(newest.text);
  await expect(save(page)).toBeDisabled();
});

test('原话编辑器隐藏到模拟组件干净项目或首页时仍提示退出，取消退出可恢复草稿', async ({ page }) => {
  const value = await seed(page, '隐藏原话编辑器仍保护草稿');
  const clean = await seed(page, '没有原话草稿的干净项目');
  await open(page, value);
  await words(page).fill('未保存原话需要全局退出提醒');
  async function cancelExit() {
    const pending = page.waitForEvent('dialog');
    await page.evaluate(() => { setTimeout(() => location.reload(), 0); });
    const dialog = await pending;
    expect(dialog.type()).toBe('beforeunload');
    await dialog.dismiss();
  }
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('simulated-tone');
  await expect(editor(page)).toHaveCount(0);
  await cancelExit();
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('cosyvoice3-local');
  await expect(words(page)).toHaveValue('未保存原话需要全局退出提醒');
  await chooseProject(page, clean);
  await expect(words(page)).toHaveValue(clean.transcript.text);
  await expect(save(page)).toBeDisabled();
  await cancelExit();
  await chooseProject(page, value);
  await expect(words(page)).toHaveValue('未保存原话需要全局退出提醒');
  await page.getByRole('link', { name: '关闭项目详情', exact: true }).click();
  await expect(editor(page)).toHaveCount(0);
  await expect(page.getByRole('region', { name: '文案工作台', exact: true })).toHaveCount(0);
  await cancelExit();
  await chooseProject(page, value);
  await expect(words(page)).toHaveValue('未保存原话需要全局退出提醒');
  await saveWords(page, value);
  await expect(page.getByRole('status', { name: '片段原话状态' })).toContainText('片段原话已保存');
  await expect(save(page)).toBeDisabled();
  const unexpectedDialogs: string[] = [];
  page.on('dialog', async (dialog) => { unexpectedDialogs.push(dialog.type()); await dialog.dismiss(); });
  await page.reload();
  await page.getByLabel('选择生成组件', { exact: true }).selectOption('cosyvoice3-local');
  await expect(words(page)).toHaveValue('未保存原话需要全局退出提醒');
  expect(unexpectedDialogs).toEqual([]);
});

test('双窗口原话保存冲突保留输入，取消重载保留本地稿，明确接受才读取新版', async ({ page, context }) => {
  const value = await seed(page, '原话双窗口冲突');
  await open(page, value);
  const second = await context.newPage();
  try {
    await open(second, value);
    await words(page).fill('第一个窗口保存的准确原话');
    await words(second).fill('第二个窗口仍未保存的文字');
    await saveWords(page, value);
    await second.route(`**${transcriptURL(value)}`, (route) => route.fulfill({
      status: 503, contentType: 'application/json', body: JSON.stringify({
        error: { code: 'storage_unavailable', message: '测试注入：原话写入暂不可用' },
      }),
    }), { times: 1 });
    await saveWords(second, value, 503);
    await expect(editor(second).getByRole('alert')).toContainText('测试注入：原话写入暂不可用');
    await expect(words(second)).toHaveValue('第二个窗口仍未保存的文字');
    expect((await readTranscript(second, value)).text).toBe('第一个窗口保存的准确原话');
    await saveWords(second, value, 409);
    await expect(editor(second).getByRole('alert')).toContainText('参考原话已更新');
    await expect(words(second)).toHaveValue('第二个窗口仍未保存的文字');
    expect((await readTranscript(second, value)).text).toBe('第一个窗口保存的准确原话');
    await answerReload(second, false);
    await expect(words(second)).toHaveValue('第二个窗口仍未保存的文字');
    await expect(save(second)).toBeEnabled();
    await answerReload(second, true);
    await expect(words(second)).toHaveValue('第一个窗口保存的准确原话');
    await expect(words(second)).toBeEnabled();
    await expect(save(second)).toBeDisabled();
    await expect(editor(second).getByRole('alert')).toHaveCount(0);
  } finally { await second.close(); }
});

for (const outcome of ['success', 'failure'] as const) {
  test(`原话重载在途锁定编辑保存和重复读取，${outcome === 'success' ? '成功后读取服务端版本' : '读取失败仍保留本地稿并可继续保存'}`, async ({ page }) => {
    const value = await seed(page, `原话重载 ${outcome}`);
    await open(page, value);
    const localText = `同意重新载入之前的本地稿 ${outcome}`;
    await words(page).fill(localText);
    const captured = deferred();
    const release = deferred();
    const delivered = deferred();
    await page.route(`**${transcriptURL(value)}`, async (route) => {
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      captured.resolve();
      await release.promise;
      if (outcome === 'success') await route.fulfill({ response });
      else await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({
        error: { code: 'storage_unavailable', message: '测试注入：原话读取暂不可用' },
      }) });
      delivered.resolve();
    }, { times: 1 });
    try {
      await answerReload(page, true);
      await captured.promise;
      await expect(words(page)).toBeDisabled();
      await expect(save(page)).toBeDisabled();
      await expect(reload(page)).toBeDisabled();
      await expect(editor(page)).toContainText('正在读取片段原话');
      await expect(words(page)).toHaveValue(localText);
      await expect(realPanel(page).getByRole('button', { name: '提交真实全文任务', exact: true })).toBeDisabled();
    } finally { release.resolve(); }
    await delivered.promise;
    await expect(words(page)).toBeEnabled();
    await expect(reload(page)).toBeEnabled();
    if (outcome === 'success') {
      await expect(words(page)).toHaveValue(value.transcript.text);
      await expect(save(page)).toBeDisabled();
      await expect(editor(page).getByRole('alert')).toHaveCount(0);
    } else {
      await expect(words(page)).toHaveValue(localText);
      await expect(save(page)).toBeEnabled();
      await expect(editor(page).getByRole('alert')).toContainText('测试注入：原话读取暂不可用');
      expect((await readTranscript(page, value)).text).toBe(value.transcript.text);
      await saveWords(page, value);
      await expect(editor(page).getByRole('alert')).toHaveCount(0);
      expect((await readTranscript(page, value)).text).toBe(localText);
    }
  });
}

for (const operation of ['GET', 'PUT'] as const) {
  for (const destination of ['clip', 'project'] as const) {
    test(`迟到原话 ${operation} 不能覆盖切换${destination === 'clip' ? '片段' : '项目'}后的输入`, async ({ page }) => {
      const source = await seed(page, `原话迟到 ${operation} ${destination}`);
      let target: Seed;
      if (destination === 'project') target = await seed(page, '原话切换目标项目');
      else {
        const response = await page.request.post(`/api/projects/${source.project.id}/references/${source.referenceId}/clips`, {
          data: { start_ms: 200, end_ms: 900 },
        });
        expect(response.status()).toBe(201);
        target = { ...source, clipId: (await response.json() as { id: string }).id };
      }
      target.transcript = await writeTranscript(page, target, '目标片段自己的服务端原话');
      const activate = await page.request.put(`/api/projects/${source.project.id}/active-clip`, { data: { clip_id: source.clipId } });
      expect(activate.status()).toBe(200);
      await open(page, source);
      const captured = deferred();
      const release = deferred();
      const delivered = deferred();
      await page.route(`**${transcriptURL(source)}`, async (route) => {
        if (route.request().method() !== operation) { await route.continue(); return; }
        const response = await route.fetch();
        expect(response.status()).toBe(200);
        captured.resolve();
        await release.promise;
        await route.fulfill({ response });
        delivered.resolve();
      }, { times: 1 });
      try {
        if (operation === 'GET') await reload(page).click();
        else {
          await words(page).fill('来源片段已经写入服务器的新版原话');
          await save(page).click();
        }
        await captured.promise;
        if (destination === 'project') {
          await page.getByRole('region', { name: '项目列表' }).locator(`a[href="#project=${target.project.id}"]`).click();
          await expect(page.getByRole('region', { name: '项目详情', exact: true }).getByRole('heading', { name: target.project.name, exact: true })).toBeVisible();
          await page.getByLabel('选择生成组件', { exact: true }).selectOption('cosyvoice3-local');
        } else {
          await page.getByLabel('选择试听片段', { exact: true }).selectOption(target.clipId);
          await page.getByRole('button', { name: '设为当前参考', exact: true }).click();
        }
        await expect(words(page)).toHaveValue(target.transcript.text);
        await expect(words(page)).toBeEnabled();
        await words(page).fill('目标片段保留的本地未保存文字');
      } finally { release.resolve(); }
      await delivered.promise;
      await rendered(page);
      await expect(words(page)).toHaveValue('目标片段保留的本地未保存文字');
      await expect(save(page)).toBeEnabled();
      await expect(editor(page).getByRole('alert')).toHaveCount(0);
      await expect(editor(page)).toContainText('片段原话尚未保存');
      expect((await readTranscript(page, target)).text).toBe(target.transcript.text);
      expect((await readTranscript(page, source)).text).toBe(operation === 'PUT'
        ? '来源片段已经写入服务器的新版原话' : source.transcript.text);
    });
  }
}
