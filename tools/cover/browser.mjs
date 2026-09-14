import { mkdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { parseArgs } from 'node:util';

export function outputPath(root, description) {
  let options;
  try {
    options = parseArgs({ options: { help: { type: 'boolean', short: 'h' }, output: { type: 'string' } } }).values;
  } catch (error) {
    console.error(`${error.message}; see --help`);
    process.exit(2);
  }
  if (options.help) {
    console.log(`Usage: npm --prefix tools/cover run capture -- [--output FILE]\n\n${description}\n\nInstall capture dependencies with npm ci --prefix tools/cover, then\nnpm exec --prefix tools/cover -- playwright install chromium.\nDefault output: docs/screenshots/cover.png\nExit status: 0 success, 1 capture failure, 2 usage, 3 missing dependency.`);
    process.exit(0);
  }
  return resolve(root, options.output ?? 'docs/screenshots/cover.png');
}

export async function capture({ output, html, url, ready, setup, viewport = { width: 1440, height: 960 }, colorScheme = 'dark' }) {
  let chromium;
  try { ({ chromium } = await import('playwright')); }
  catch { console.error('Missing capture dependencies; run npm ci --prefix tools/cover'); process.exit(3); }
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport, deviceScaleFactor: 1, locale: 'en-US', timezoneId: 'UTC', colorScheme, reducedMotion: 'reduce' });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => route.abort());
    if (setup) await setup(page);
    if (html !== undefined) await page.setContent(html);
    else await page.goto(url, { waitUntil: 'networkidle', timeout: 60_000 });
    await ready(page);
    await page.evaluate(() => document.fonts.ready);
    if (errors.length) throw new Error(errors.join('\n'));
    await mkdir(dirname(output), { recursive: true });
    await page.screenshot({ path: output, animations: 'disabled' });
    console.log(`Captured ${output}`);
  } finally { await browser.close(); }
}

