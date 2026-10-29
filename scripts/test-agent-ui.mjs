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
			'$app/navigation': path.resolve('test/mocks/app-navigation.ts'),
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
server.middlewares.use('/__agent_transcript', async (_req, res) => {
	const html = await server.transformIndexHtml(
		'/__agent_transcript',
		'<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module">import {mount} from "svelte";import Harness from "/test/fixtures/AgentTranscriptHarness.svelte";mount(Harness,{target:document.getElementById("app")});</script></body></html>'
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
		assert.equal(calls.find((call) => call.action === 'reply').custom, undefined);
		state.requests = [
			{
				id: 'custom-language',
				method: 'select',
				title: 'Choose language',
				options: ['Python', 'JavaScript', '其他']
			}
		];
		await page.reload();
		await page.getByRole('radio', { name: '其他', exact: true }).check();
		const customAnswer = page.getByRole('textbox', { name: 'Your answer', exact: true });
		const submit = page.getByRole('button', { name: 'Submit', exact: true });
		assert.equal(await page.getByRole('radio').count(), 3);
		assert.ok(await submit.isDisabled());
		await customAnswer.fill('Rust with 中文注释');
		await page.waitForTimeout(1200);
		assert.equal(await customAnswer.inputValue(), 'Rust with 中文注释');
		await page.getByRole('radio', { name: 'Python', exact: true }).check();
		await customAnswer.waitFor({ state: 'hidden' });
		await page.getByRole('radio', { name: '其他', exact: true }).check();
		assert.equal(await customAnswer.inputValue(), 'Rust with 中文注释');
		await customAnswer.fill('   \n ');
		assert.ok(await submit.isDisabled());
		await customAnswer.fill('  Rust with 中文注释  ');
		rejectReply = true;
		await submit.click();
		await page.getByRole('alert').filter({ hasText: 'Permission could not be saved' }).waitFor();
		assert.equal(await customAnswer.inputValue(), '  Rust with 中文注释  ');
		rejectReply = false;
		await submit.click();
		await page.waitForFunction(() => !document.querySelector('input[type=radio]'));
		assert.equal(
			calls.filter((call) => call.id === 'custom-language').at(-1).value,
			'Rust with 中文注释'
		);
		assert.equal(calls.filter((call) => call.id === 'custom-language').at(-1).custom, true);
		state.requests = [
			{ id: 'custom-cancel', method: 'select', title: 'Choose language', options: ['Python'] }
		];
		await page.reload();
		await page.getByRole('radio', { name: 'Other', exact: true }).check();
		await customAnswer.fill('Do not send this answer');
		await page.getByRole('button', { name: 'Cancel', exact: true }).click();
		await page.waitForFunction(() => !document.querySelector('input[type=radio]'));
		assert.equal(calls.filter((call) => call.id === 'custom-cancel').at(-1).cancelled, true);
		assert.equal(calls.filter((call) => call.id === 'custom-cancel').at(-1).value, undefined);
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
			const labels = {
				'zh-CN': { other: '其他', answer: '你的答复', submit: '提交' },
				'zh-TW': { other: '其他（请填写）', answer: '你的答覆', submit: '提交' },
				'es-ES': { other: 'Otro', answer: 'Tu respuesta', submit: 'Enviar' }
			}[language];
			state.requests = [
				{
					id: `custom-${language}`,
					method: 'select',
					title: 'Choose language',
					options: ['Python', labels.other]
				}
			];
			await page.goto(`${base}?lang=${language}`);
			await page.getByRole('radio', { name: labels.other, exact: true }).check();
			assert.equal(await page.getByRole('radio').count(), 2);
			await page.getByRole('textbox', { name: labels.answer, exact: true }).fill('Rust');
			await page.getByRole('button', { name: /^Agent/ }).waitFor();
			await page.getByRole('button', { name: /^Agent/ }).click();
			await page.screenshot({ path: `.tmp/agent-ui/${width}-${language}.png` });
			assert.ok(
				await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
				`${language}: horizontal overflow`
			);
			await page
				.locator('form')
				.filter({ has: page.getByRole('radio') })
				.getByRole('button', { name: labels.submit, exact: true })
				.click();
			await page.waitForFunction(() => !document.querySelector('input[type=radio]'));
			assert.equal(calls.filter((call) => call.id === `custom-${language}`).at(-1).value, 'Rust');
			assert.equal(calls.filter((call) => call.id === `custom-${language}`).at(-1).custom, true);
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
		await page.goto(base.replace('/__agent_test', '/__agent_transcript'));
		await page.waitForFunction(() => typeof window.setAgentTranscript === 'function');
		const transcript = [
			{ id: 'start', type: 'message', content: [{ type: 'output_text', text: '先检查文件。' }] },
			{
				id: 'read',
				type: 'function_call',
				call_id: 'read',
				name: 'read',
				status: 'completed',
				arguments: { path: 'example.txt' }
			},
			{
				id: 'read-output',
				type: 'function_call_output',
				call_id: 'read',
				status: 'completed',
				output: [{ text: 'Found the configuration.' }]
			},
			{
				id: 'next',
				type: 'message',
				content: [{ type: 'output_text', text: '找到原因了，接着运行测试。' }]
			},
			{
				id: 'test',
				type: 'function_call',
				call_id: 'test',
				name: 'bash',
				status: 'in_progress',
				arguments: { command: 'npm test' }
			},
			{
				id: 'test-output',
				type: 'function_call_output',
				call_id: 'test',
				status: 'in_progress',
				output: [{ text: 'One test passed, still running.' }]
			}
		];
		await page.evaluate((output) => window.setAgentTranscript(output), transcript);
		await page.getByText('先检查文件。', { exact: true }).waitFor();
		await page.getByText('找到原因了，接着运行测试。', { exact: true }).waitFor();
		await page.getByText('npm test', { exact: true }).waitFor();
		await page.getByText('One test passed, still running.', { exact: true }).waitFor();
		assert.equal(await page.locator('#agent-transcript-detail-3-tool-call .shimmer').count(), 1);
		await page.screenshot({ path: `.tmp/agent-ui/${width}-transcript-running.png` });
		transcript[4].status = transcript[5].status = 'completed';
		transcript[5].output[0].text = 'All tests passed.';
		transcript.push({
			id: 'done',
			type: 'message',
			content: [{ type: 'output_text', text: '修复完成，测试通过。' }]
		});
		await page.evaluate((output) => window.setAgentTranscript(output, true), transcript);
		await page.getByText('All tests passed.', { exact: true }).waitFor();
		await page.getByText('修复完成，测试通过。', { exact: true }).waitFor();
		await page.reload();
		await page.getByText('修复完成，测试通过。', { exact: true }).waitFor();
		assert.equal(await page.getByText('先检查文件。', { exact: true }).count(), 1);
		const ordered = await page.locator('main').innerText();
		assert.ok(ordered.indexOf('先检查文件。') < ordered.indexOf('example.txt'));
		assert.ok(ordered.indexOf('example.txt') < ordered.indexOf('找到原因了'));
		assert.ok(ordered.indexOf('找到原因了') < ordered.indexOf('npm test'));
		assert.ok(ordered.indexOf('npm test') < ordered.indexOf('修复完成'));
		assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
		await page.screenshot({ path: `.tmp/agent-ui/${width}-transcript-completed.png` });
		assert.deepEqual(errors, []);
		await page.close();
	}
	console.log(
		'Agent UI: choices, custom answers, validation, failed submission retry, cancellation, reconnect, refusal, interleaved transcript, live tool output, transcript reload, search, steering, auto-compaction, four languages and mobile/desktop passed.'
	);
} finally {
	await browser?.close();
	await server.close();
}
