import { expect, test, type Page, type Locator } from './advancedFixture';
import { mkdtemp, open, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import type { ReferenceAudio } from '../src/ReferencePanel';

// A synthetic sine wave, generated in memory. No person's audio is committed or uploaded.
function tone(duration = 2, sampleRate = 24000): Buffer {
  const frames = Math.round(duration * sampleRate);
  const bytes = frames * 2;
  const result = Buffer.alloc(44 + bytes);
  result.write('RIFF', 0); result.writeUInt32LE(36 + bytes, 4); result.write('WAVEfmt ', 8);
  result.writeUInt32LE(16, 16); result.writeUInt16LE(1, 20); result.writeUInt16LE(1, 22);
  result.writeUInt32LE(sampleRate, 24); result.writeUInt32LE(sampleRate * 2, 28);
  result.writeUInt16LE(2, 32); result.writeUInt16LE(16, 34); result.write('data', 36); result.writeUInt32LE(bytes, 40);
  for (let index = 0; index < frames; index += 1) result.writeInt16LE(Math.round(6000 * Math.sin(2 * Math.PI * 440 * index / sampleRate)), 44 + index * 2);
  return result;
}
async function project(page: Page, suffix: string) {
  const response = await page.request.post('/api/projects', { data: { name: `参考音频 ${suffix}` } });
  expect(response.status()).toBe(201);
  const value = await response.json() as { id: string; name: string };
  await page.goto(`/#project=${value.id}`);
  await expect(page.getByLabel('导入参考音频', { exact: true })).toBeEnabled();
  return value;
}
async function upload(page: Page, name = '人工合成短音.wav', buffer = tone()) {
  await page.getByLabel('导入参考音频', { exact: true }).setInputFiles({ name, mimeType: 'audio/wav', buffer });
  await page.getByRole('button', { name: '上传音频', exact: true }).click();
  await expect(page.getByRole('status', { name: '素材操作状态' })).toContainText('音频已保存在本机');
}
async function save(page: Page, start: string, end: string) {
  await page.getByLabel('开始（秒）', { exact: true }).fill(start);
  await page.getByLabel('结束（秒）', { exact: true }).fill(end);
  await page.getByRole('button', { name: '保存为当前参考' }).click();
  await expect(page.getByRole('status', { name: '素材操作状态' })).toContainText('新片段已保存');
}
async function playAndSeek(player: Locator, duration: number) {
  await expect.poll(() => player.evaluate((element) => (element as HTMLAudioElement).readyState)).toBeGreaterThanOrEqual(2);
  expect(await player.evaluate((element) => (element as HTMLAudioElement).duration)).toBeCloseTo(duration, 2);
  await player.evaluate(async (element) => { await (element as HTMLAudioElement).play(); });
  await expect.poll(() => player.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeGreaterThan(0.05);
  await player.evaluate((element) => { const audio = element as HTMLAudioElement; audio.pause(); audio.currentTime = audio.duration / 2; });
  await expect.poll(() => player.evaluate((element) => (element as HTMLAudioElement).currentTime)).toBeCloseTo(duration / 2, 2);
}

test('上传与实际播放、精确选段、保留旧版、切换和刷新恢复', async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const current = await project(page, `完整流程-${testInfo.project.name}`);
  const bytes = tone();
  await upload(page, '人工合成短音.wav', bytes);
  await expect(page.getByText('尚未选择当前参考片段', { exact: true })).toBeVisible();
  await playAndSeek(page.getByLabel('完整参考音频', { exact: true }), 2);
  const library = await (await page.request.get(`/api/projects/${current.id}/references`)).json() as { references: ReferenceAudio[] };
  const source = library.references[0];
  const original = await page.request.get(`/api/projects/${current.id}/references/${source.id}/original`);
  expect(await original.body()).toEqual(bytes);
  const ranged = await page.request.get(`/api/projects/${current.id}/references/${source.id}/preview`, { headers: { Range: 'bytes=0-43' } });
  expect(ranged.status()).toBe(206);
  expect((await ranged.body()).length).toBe(44);

  await save(page, '0.125', '1.375');
  await playAndSeek(page.getByLabel('已保存参考片段', { exact: true }), 1.25);
  const choices = page.getByLabel('选择试听片段', { exact: true });
  const first = await choices.inputValue();
  await save(page, '0.500', '1.000');
  await expect(choices.locator('option')).toHaveCount(3);
  const second = await choices.inputValue();
  expect(second).not.toBe(first);
  await choices.selectOption(first);
  await playAndSeek(page.getByLabel('已保存参考片段', { exact: true }), 1.25);
  await expect(page.getByText('仅试听，尚未设为当前参考')).toBeVisible();
  await page.getByRole('button', { name: '设为当前参考', exact: true }).click();
  await expect(page.getByRole('status', { name: '素材操作状态' })).toHaveText('当前参考片段已切换。');
  await page.reload();
  await expect(choices).toHaveValue(first);
  await expect(page.getByText('当前参考片段', { exact: true })).toBeVisible();
  await expect(page.getByLabel('开始（秒）', { exact: true })).toHaveValue('0.125');
  expect(await (await page.request.get(`/api/projects/${current.id}/references/${source.id}/original`)).body()).toEqual(bytes);
  await upload(page, '人工合成短音.wav', bytes);
  await expect(page.getByRole('navigation', { name: '参考素材列表' }).getByRole('button')).toHaveCount(2);
  expect(await (await page.request.get(`/api/projects/${current.id}/references/${source.id}/original`)).body()).toEqual(bytes);
  expect(errors).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('坏文件、空文件、超限文件与无效选段明确报错', async ({ page }, testInfo) => {
  const current = await project(page, `错误边界-${testInfo.project.name}`);
  const input = page.getByLabel('导入参考音频', { exact: true });
  const submit = page.getByRole('button', { name: '上传音频', exact: true });
  const panel = page.getByRole('region', { name: '参考音频管理' });
  await input.setInputFiles({ name: 'bad.wav', mimeType: 'audio/wav', buffer: Buffer.from('this is not an audio file') });
  await submit.click();
  await expect(panel.getByRole('alert')).toBeVisible();
  await expect(page.getByRole('status', { name: '素材操作状态' })).not.toContainText('已保存');
  await input.setInputFiles({ name: 'empty.wav', mimeType: 'audio/wav', buffer: Buffer.alloc(0) });
  await submit.click();
  await expect(panel.getByRole('alert')).toContainText('非空');
  const temporary = await mkdtemp(path.join(tmpdir(), 'voice-upload-test-'));
  try {
    const filename = path.join(temporary, 'oversize.wav');
    const oversized = await open(filename, 'w');
    await oversized.truncate(50 * 1024 * 1024 + 1);
    await oversized.close();
    await input.setInputFiles(filename);
    await submit.click();
    await expect(panel.getByRole('alert')).toContainText('50 MiB');
  } finally { await rm(temporary, { recursive: true, force: true }); }
  expect((await (await page.request.get(`/api/projects/${current.id}/references`)).json()).references).toHaveLength(0);
  await upload(page);
  const saveButton = page.getByRole('button', { name: '保存为当前参考' });
  for (const [start, end] of [['1', '1'], ['1.5', '1'], ['0', '3'], ['0', '0.099'], ['', '1'], ['0.0001', '1']]) {
    await page.getByLabel('开始（秒）', { exact: true }).fill(start);
    await page.getByLabel('结束（秒）', { exact: true }).fill(end);
    await expect(saveButton).toBeDisabled();
  }
});

test('上传与保存失败保留输入和旧参考，可重试', async ({ page }, testInfo) => {
  const current = await project(page, `失败重试-${testInfo.project.name}`);
  const panel = page.getByRole('region', { name: '参考音频管理' });
  await page.route('**/references?filename=*', (route) => route.abort());
  await page.getByLabel('导入参考音频', { exact: true }).setInputFiles({ name: 'retry.wav', mimeType: 'audio/wav', buffer: tone() });
  await page.getByRole('button', { name: '上传音频', exact: true }).click();
  await expect(panel.getByRole('alert')).toContainText('暂时无法连接');
  expect(await page.getByLabel('导入参考音频', { exact: true }).evaluate((element) => (element as HTMLInputElement).files?.[0]?.name)).toBe('retry.wav');
  await page.unroute('**/references?filename=*');
  await page.getByRole('button', { name: '上传音频', exact: true }).click();
  await expect(page.getByRole('status', { name: '素材操作状态' })).toContainText('音频已保存在本机');
  await save(page, '0', '1');
  const initial = await (await page.request.get(`/api/projects/${current.id}/references`)).json() as { active_clip_id: string };
  await page.route('**/references/*/clips', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { message: '本地存储暂不可用' } }) }));
  await page.getByLabel('开始（秒）', { exact: true }).fill('0.5');
  await page.getByLabel('结束（秒）', { exact: true }).fill('1.5');
  await page.getByRole('button', { name: '保存为当前参考' }).click();
  await expect(panel.getByRole('alert')).toContainText('本地存储暂不可用');
  await expect(page.getByLabel('开始（秒）', { exact: true })).toHaveValue('0.5');
  await expect(page.getByRole('status', { name: '素材操作状态' })).not.toContainText('已保存');
  expect((await (await page.request.get(`/api/projects/${current.id}/references`)).json()).active_clip_id).toBe(initial.active_clip_id);
  await page.unroute('**/references/*/clips');
  await page.getByRole('button', { name: '保存为当前参考' }).click();
  await expect(page.getByRole('status', { name: '素材操作状态' })).toContainText('新片段已保存');
});

test('素材读取失败可恢复；迟到上传和保存响应不会串到另一个项目', async ({ page }, testInfo) => {
  const first = await project(page, `原项目-${testInfo.project.name}`);
  const second = await (await page.request.post('/api/projects', { data: { name: `新项目-${testInfo.project.name}` } })).json() as { id: string; name: string };
  await page.route(`**/api/projects/${first.id}/references`, (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { message: '素材暂不可用' } }) }));
  await page.reload();
  await expect(page.getByRole('region', { name: '参考音频管理' }).getByRole('alert')).toContainText('素材暂不可用');
  await expect(page.getByLabel('导入参考音频', { exact: true })).toBeDisabled();
  await page.unroute(`**/api/projects/${first.id}/references`);
  await page.getByRole('button', { name: '重试读取素材' }).click();
  await expect(page.getByLabel('导入参考音频', { exact: true })).toBeEnabled();
  for (const action of ['upload', 'save']) {
    let release!: () => void;
    let captured!: () => void;
    let delivered!: () => void;
    const wait = new Promise<void>((resolve) => { release = resolve; });
    const ready = new Promise<void>((resolve) => { captured = resolve; });
    const done = new Promise<void>((resolve) => { delivered = resolve; });
    const url = action === 'upload' ? '**/references?filename=*' : '**/references/*/clips';
    await page.route(url, async (route) => { const response = await route.fetch(); captured(); await wait; await route.fulfill({ response }); delivered(); }, { times: 1 });
    if (action === 'upload') {
      await page.getByLabel('导入参考音频', { exact: true }).setInputFiles({ name: 'late.wav', mimeType: 'audio/wav', buffer: tone() });
      await page.getByRole('button', { name: '上传音频', exact: true }).click();
    } else {
      await page.getByLabel('开始（秒）', { exact: true }).fill('0.25');
      await page.getByLabel('结束（秒）', { exact: true }).fill('1.25');
      await page.getByRole('button', { name: '保存为当前参考' }).click();
    }
    await ready;
    await expect(page.getByRole('button', { name: '刷新素材', exact: true })).toBeDisabled();
    await page.getByRole('region', { name: '项目列表' }).getByRole('link').filter({ has: page.getByRole('heading', { name: second.name, exact: true }) }).click();
    await expect(page.getByRole('region', { name: '参考音频管理' }).getByText(`为「${second.name}」挑选一段声音，保留原件，随时重新选取。`)).toBeVisible();
    release(); await done;
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(page.getByText('把参考声音放进来', { exact: true })).toBeVisible();
    expect((await (await page.request.get(`/api/projects/${second.id}/references`)).json()).references).toHaveLength(0);
    await page.getByRole('region', { name: '项目列表' }).getByRole('link').filter({ has: page.getByRole('heading', { name: first.name, exact: true }) }).click();
    await expect(page.getByRole('heading', { name: 'late.wav', exact: true })).toBeVisible();
  }
  await expect(page.getByText('当前参考片段', { exact: true })).toBeVisible();
});
