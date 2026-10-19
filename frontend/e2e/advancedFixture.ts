import { test as base } from '@playwright/test';
export * from '@playwright/test';
export const test = base.extend({
  page: async ({ page }, use) => {
    await page.addInitScript(() => {
      localStorage.setItem('voice-advanced', 'true');
      localStorage.setItem('voice-adapter', 'simulated-tone');
    });
    await use(page);
  },
});
