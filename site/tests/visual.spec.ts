// Screenshots in light and dark for a human review. Not a pass/fail check: run with SCREENSHOTS=1.
import { test } from '@playwright/test';

const BASE = '/psr-ai/';
test.skip(!process.env.SCREENSHOTS, 'set SCREENSHOTS=1 to capture screenshots');

for (const scheme of ['light', 'dark'] as const) {
  for (const route of ['', 'getting-started/', 'limitations/']) {
    test(`${scheme} /${route}`, async ({ page }, info) => {
      await page.emulateMedia({ colorScheme: scheme });
      await page.goto(BASE + route);
      await page.screenshot({ path: info.outputPath(`${scheme}.png`) });
    });
  }
}
