import { defineConfig } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

const root = fileURLToPath(new URL('..', import.meta.url));
const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const port = process.env.VOICE_E2E_PORT ?? '18765';
const baseURL = `http://127.0.0.1:${port}`;
// Workers reload this config; inherit one run ID so all artifacts share the same run directory.
const runId = process.env.VOICE_E2E_RUN_ID ?? randomUUID();
process.env.VOICE_E2E_RUN_ID = runId;

export default defineConfig({
  testDir: './e2e',
  outputDir: path.join(root, '.quality-local', 'playwright-results', runId),
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: 'list',
  use: { baseURL, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [
    { name: 'desktop', use: { browserName: 'chromium', viewport: { width: 1365, height: 1000 } } },
    { name: 'mobile', use: { browserName: 'chromium', viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
  webServer: {
    command: `"${python}" "${path.join(root, 'scripts/e2e_server.py')}"`,
    url: `${baseURL}/api/health`,
    reuseExistingServer: false,
    timeout: 20000,
  },
});
