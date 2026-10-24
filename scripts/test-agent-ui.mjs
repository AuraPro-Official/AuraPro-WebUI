import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import path from 'node:path';
import fs from 'node:fs/promises';
import { createServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const server = await createServer({
	configFile: false,
	root: process.cwd(),
	plugins: [svelte({ configFile: false })],
	define: { APP_VERSION: JSON.stringify('test'), APP_BUILD_HASH: JSON.stringify('test') },
	resolve: {
		alias: {
			$lib: path.resolve('src/lib'),
			'$app/environment': path.resolve('test/mocks/app-environment.ts')
		}
	},
	server: { host: '127.0.0.1', port: 0 },
	appType: 'custom'
});
server.middlewares.use('/__agent_test', async (_req, res) => {
	const html = await server.transformIndexHtml(
		'/__agent_test',
		'<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module">import {mount} from "svelte";import Harness from "/test/fixtures/AgentWorkspaceHarness.svelte";mount(Harness,{target:document.getElementById("app")});</script></body></html>'
	);
	res.setHeader('Content-Type', 'text/html');
	res.end(html);
});
await server.listen();
let browser;
try {
	browser = await chromium.launch({
		headless: true,
		...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
			? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
			: {})
	});
	const base = `http://127.0.0.1:${server.httpServer.address().port}/__agent_test`;
	await fs.mkdir('.tmp/agent-ui', { recursive: true });
	for (const width of [390, 1280]) {
		const page = await browser.newPage({ viewport: { width, height: 1000 } });
		const errors = [];
		page.on('pageerror', (error) => errors.push(error.message));
		const calls = [];
		let rejectReply = false;
		let state = {
			session_id: 'session-test',
			busy: true,
			compacting: false,
			auto_compaction: true,
			cursor: 2,
			items: [
				{ id: 1, time: 1700000000, kind: 'started' },
				{
					id: 2,
					time: 1700000001,
					kind: 'tool',
					name: 'bash',
					input: { command: 'npm test' },
					output: 'Test output',
					status: 'completed'
				}
			],
			requests: [
				{ id: 'choose', method: 'select', title: 'Choose output format', options: ['JSON', 'CSV'] }
			],
			tools: [
				{
					id: 'tool-1',
					name: 'bash',
					input: { command: 'npm test' },
					output: 'Live terminal output',
					status: 'running'
				}
			],
			todos: [{ id: 'step-1', content: 'Verify changes', status: 'in_progress' }],
			queue: { steering: [], followUp: [] },
			stats: { contextUsage: { tokens: 1234, contextWindow: 32768, percent: 4 } }
		};
		await page.route('**/api/v1/opencode/**', async (route) => {
			if (route.request().method() === 'POST') {
				const body = route.request().url().endsWith('/abort')
					? { action: 'abort' }
					: route.request().postDataJSON();
				calls.push(body);
				if (rejectReply && body.action === 'reply') {
					await route.fulfill({ status: 400, json: { detail: 'Permission could not be saved' } });
					return;
				}
				if (body.action === 'reply') state.requests = [];
				await route.fulfill({ json: { ok: true } });
			} else {
				const after = Number(new URL(route.request().url()).searchParams.get('after') || 0);
				await route.fulfill({
					json: { ...state, items: state.items.filter((item) => item.id > after) }
				});
			}
		});
		await page.goto(base);
		await page.getByRole('group', { name: 'Choose output format' }).waitFor();
		await page.screenshot({ path: `.tmp/agent-ui/${width}-choices.png` });
		await page.getByLabel('CSV', { exact: true }).check();
		await page.getByRole('button', { name: 'Submit', exact: true }).click();
		await page.waitForFunction(() => !document.querySelector('input[type=radio]'));
		assert.equal(calls.find((call) => call.action === 'reply').value, 'CSV');
		await page.getByRole('button', { name: /^Agent/ }).click();
		await page.getByRole('button', { name: 'Tool results', exact: true }).click();
		await page.getByText('Live terminal output', { exact: true }).waitFor();
		await page.getByRole('button', { name: 'Activity', exact: true }).click();
		await page.getByRole('searchbox', { name: 'Search activity' }).fill('npm test');
		assert.equal(await page.locator('section details').count(), 1);
		await page
			.getByRole('textbox', { name: 'New instruction', exact: true })
			.fill('Also run tests');
		await page.getByRole('button', { name: 'Send', exact: true }).click();
		await page.waitForFunction(
			() => document.querySelector('textarea[aria-label="New instruction"]').value === ''
		);
		assert.equal(calls.find((call) => call.action === 'steer').message, 'Also run tests');
		await page.getByRole('checkbox', { name: 'Auto compact context' }).uncheck();
		await page.waitForTimeout(200);
		assert.equal(calls.find((call) => call.action === 'auto_compaction').enabled, false);
		state.requests = [
			{ id: 'confirm', method: 'confirm', title: 'Write file?', message: 'example.txt' }
		];
		await page.reload();
		rejectReply = true;
		await page.getByRole('button', { name: 'Approve', exact: true }).click();
		await page.getByRole('alert').filter({ hasText: 'Permission could not be saved' }).waitFor();
		assert.equal(state.requests.length, 1);
		rejectReply = false;
		await page.getByRole('button', { name: 'Decline', exact: true }).click();
		assert.equal(calls.filter((call) => call.action === 'reply').at(-1).confirmed, false);
		for (const language of ['zh-CN', 'zh-TW', 'es-ES']) {
			await page.goto(`${base}?lang=${language}`);
			await page.getByRole('button', { name: /^Agent/ }).waitFor();
			await page.getByRole('button', { name: /^Agent/ }).click();
			await page.screenshot({ path: `.tmp/agent-ui/${width}-${language}.png` });
			assert.ok(
				await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
				`${language}: horizontal overflow`
			);
		}
		state.busy = false;
		await page.goto(`${base}?active=0`);
		await page.getByRole('button', { name: 'Compact context now', exact: true }).click();
		await page.waitForTimeout(100);
		assert.ok(calls.some((call) => call.action === 'compact'));
		state.busy = true;
		await page.reload();
		await page.getByRole('button', { name: 'Stop Agent', exact: true }).click();
		await page.waitForTimeout(100);
		assert.ok(calls.some((call) => call.action === 'abort'));
		assert.deepEqual(errors, []);
		await page.close();
	}
	console.log(
		'Agent UI: choices, reconnect, refusal, live output, search, steering, auto-compaction, four languages and mobile/desktop passed.'
	);
} finally {
	await browser?.close();
	await server.close();
}
