import { expect, test } from '@playwright/test';

const persona = { id: 'wan-qwen3-v0.11', version: '0.11', label: '万先生-Qwen3v0.11-武汉话',
  reference_source: 'persona', requires_transcript: false, is_simulated: false, output_kind: 'speech',
  modes: ['preview', 'full'], parameters: [], model_id: 'Qwen3', model_revision: 'fixture',
  config_fingerprint: 'a'.repeat(64), max_text_characters: 80 };

for (const available of [true, false]) {
  test(`人物无需上传参考，${available ? '冻结版本提交失败保留文案' : '不可用不降级'}`, async ({ page }) => {
    const project = await (await page.request.post('/api/projects', { data: { name: '人物接入' } })).json();
    await page.route('**/api/generation-adapters', route => route.fulfill({ json: { adapters: [persona] } }));
    await page.route('**/api/generation-adapters/*/health', route => route.fulfill({ json: {
      adapter_id: persona.id, configured: available, configuration_valid: available,
      execution_available: available, model_loaded: false, state: available ? 'ready' : 'unavailable', error: null,
    } }));
    const bodies: Record<string, unknown>[] = [];
    await page.route(`**/api/projects/${project.id}/tasks`, route => {
      if (route.request().method() !== 'POST') return route.continue();
      bodies.push(route.request().postDataJSON());
      return route.fulfill({ status: 409, json: { error: { code: 'persona_changed', message: '人物版本资源已改变' } } });
    });
    await page.goto(`/#project=${project.id}`);
    await expect(page.getByLabel('选择生成组件', { exact: true })).toHaveValue(persona.id);
    await page.getByLabel('要朗读的文案', { exact: true }).fill('今天到屋里看哈情况。');
    const button = page.getByRole('button', { name: '生成配音', exact: true });
    if (available) {
      await expect(button).toBeEnabled(); await button.click();
      await expect(page.getByRole('alert', { name: '任务操作错误' })).toContainText('人物版本资源已改变');
      expect(bodies).toHaveLength(1);
      expect(bodies[0]).toMatchObject({ adapter_id: persona.id, expected_clip_id: null, expected_adapter_version: '0.11', expected_config_fingerprint: persona.config_fingerprint });
      expect(bodies[0]).not.toHaveProperty('expected_transcript_revision');
      await expect(page.getByLabel('要朗读的文案', { exact: true })).toHaveValue('今天到屋里看哈情况。');
      const saved = await (await page.request.get(`/api/projects/${project.id}/text`)).json();
      expect(saved.confirmation_current).toBe(true);
      await page.reload();
      await expect(page.getByLabel('选择生成组件', { exact: true })).toHaveValue(persona.id);
    } else {
      await expect(button).toBeDisabled(); expect(bodies).toHaveLength(0);
    }
    await expect(page.getByRole('button', { name: '生成测试音（不朗读）', exact: true })).toHaveCount(0);
    await page.getByText('查看模型环境详情', { exact: true }).click();
    await expect(page.getByLabel('真实模型状态')).toContainText('已绑定固定人物参考，无需上传');
    await expect(page.getByLabel('真实模型状态')).not.toContainText('最多 30 秒');
  });
}
