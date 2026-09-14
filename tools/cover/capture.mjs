import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { capture, outputPath } from './browser.mjs';

const root = fileURLToPath(new URL('../../', import.meta.url));
const output = outputPath(root, 'Render the actual monitor templates from synthetic state without starting agents or discovering live runs.');
const scratch = await mkdtemp(join(tmpdir(), 'stream-eval-cover-'));
try {
  const rendered = join(scratch, 'dashboard.html');
  const python = process.env.COVER_PYTHON ?? 'python3';
  execFileSync(python, [join(root, 'tools/cover/render.py'), rendered], {
    cwd: root, stdio: 'pipe', timeout: 15_000, env: { ...process.env, PYTHONPATH: root, TZ: 'UTC' },
  });
  const html = await readFile(rendered, 'utf8');
  const partial = html.match(/<main>([\s\S]*?)<\/main>/)?.[1];
  assert.ok(partial, 'The dashboard must expose its main content');
  const style = await readFile(new URL('../../stream_eval/monitor/static/style.css', import.meta.url), 'utf8');
  await capture({ output, url: 'http://cover.invalid/', viewport: { width: 1600, height: 1000 },
    async setup(page) {
      await page.route('http://cover.invalid/**', async route => {
        const path = new URL(route.request().url()).pathname;
        assert.equal(route.request().method(), 'GET');
        if (path === '/') await route.fulfill({ body: new URL(route.request().url()).searchParams.get('_partial') === '1' ? partial : html, contentType: 'text/html' });
        else if (path === '/static/style.css') await route.fulfill({ body: style, contentType: 'text/css' });
        else throw new Error(`Unexpected capture request ${path}`);
      });
    },
    async ready(page) {
      await page.getByRole('heading', { name: 'stream-eval', exact: true }).waitFor();
      assert.equal(await page.locator('.row[data-status]').count(), 3);
      await page.getByText('dsc-endpoint-help', { exact: true }).waitFor();
    },
  });
} finally { await rm(scratch, { recursive: true, force: true }); }
