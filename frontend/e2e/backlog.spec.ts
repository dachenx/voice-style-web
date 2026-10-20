import { expect, test } from './advancedFixture';
import { splitText } from '../src/TextSplit';

test('分段保留文字标点，去边界空白，Unicode 按字符计数', () => {
  for (const input of ['字'.repeat(80) + '\n', '字'.repeat(80) + ' '.repeat(160) + '末', '😀'.repeat(161), '这是一句话。'.repeat(40)]) {
    const parts = splitText(input);
    expect(parts.every(p => p.trim() && Array.from(p).length <= 80)).toBe(true);
    expect(parts.join('').replace(/\s/g, '')).toBe(input.replace(/\s/g, ''));
  }
});

test('参考素材及片段二次确认删除，保留其他素材，刷新不复活', async ({ page }) => {
  const { id } = await (await page.request.post('/api/projects', { data: { name: '素材清理' } })).json();
  const base = `/api/projects/${id}`;
  const audio = Buffer.alloc(44 + 48000);
  audio.write('RIFF'); audio.writeUInt32LE(audio.length - 8, 4); audio.write('WAVEfmt ', 8);
  audio.writeUInt32LE(16, 16); audio.writeUInt16LE(1, 20); audio.writeUInt16LE(1, 22);
  audio.writeUInt32LE(24000, 24); audio.writeUInt32LE(48000, 28); audio.writeUInt16LE(2, 32); audio.writeUInt16LE(16, 34);
  audio.write('data', 36); audio.writeUInt32LE(48000, 40);
  const ref = await (await page.request.post(`${base}/references?filename=synthetic.wav`, { headers: { 'Content-Type': 'application/octet-stream' }, data: audio })).json();
  await page.request.post(`${base}/references/${ref.id}/clips`, { data: { start_ms: 0, end_ms: 800 } });
  await page.goto(`/#project=${id}`);
  await page.getByRole('button', { name: '删除这个片段', exact: true }).click();
  let dialog = page.getByRole('dialog', { name: '确认删除这个片段' });
  await expect(dialog).toBeInViewport();
  await dialog.getByRole('button', { name: '取消删除', exact: true }).click();
  await page.getByRole('button', { name: '删除这个片段', exact: true }).click();
  await dialog.getByRole('button', { name: '确认永久删除', exact: true }).click();
  await expect(page.getByRole('button', { name: '删除这个片段', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '删除参考素材', exact: true }).click();
  dialog = page.getByRole('dialog', { name: '确认删除参考素材' });
  await dialog.getByRole('button', { name: '确认永久删除', exact: true }).click();
  await expect(page.getByText('把参考声音放进来', { exact: true })).toBeVisible();
  await page.reload();
  expect((await (await page.request.get(`${base}/references`)).json()).references).toEqual([]);
});
