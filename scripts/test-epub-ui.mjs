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
	publicDir: path.resolve('static'),
	plugins: [svelte({ configFile: false })],
	define: { APP_VERSION: JSON.stringify('test'), APP_BUILD_HASH: JSON.stringify('test') },
	resolve: {
		alias: {
			'$lib/components/layout/Sidebar/UserMenu.svelte': path.resolve(
				'test/fixtures/EpubUserMenuStub.svelte'
			),
			'$lib/stores': path.resolve('test/fixtures/epub-stores.ts'),
			$lib: path.resolve('src/lib'),
			'$app/environment': path.resolve('test/mocks/app-environment.ts')
		}
	},
	server: { host: '127.0.0.1', port: 0 },
	appType: 'custom'
});
server.middlewares.use('/__epub_test', async (_req, res) => {
	res.setHeader('Content-Type', 'text/html');
	res.end(
		await server.transformIndexHtml(
			'/__epub_test',
			'<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="app"></div><script type="module">import {mount} from "svelte";import Harness from "/test/fixtures/EpubLibraryHarness.svelte";mount(Harness,{target:document.getElementById("app")});</script></body></html>'
		)
	);
});
await server.listen();
let browser;
const books = [
	{
		book_id: 'physics',
		title: 'Physics and Natural Science',
		current_version_id: 'physics-current',
		current_version_status: 'READY'
	},
	{
		book_id: 'geometry',
		title: 'Geometry and Mathematical Proofs',
		current_version_id: 'geometry-current',
		current_version_status: 'READY'
	},
	{
		book_id: 'pending',
		title: 'New Book',
		current_version_id: null,
		current_version_status: 'PARSING'
	}
];
const passage = (version, index) => ({
	passage_id: `${version}-${index}`,
	toc_path: ['Chapter 1', 'First principles'],
	content: `Passage ${index + 1}: A body remains at rest or in uniform motion unless acted upon by an external force.\nMathematical models describe the relationship between force, mass, and acceleration.`
});
const hit = (index, provenance) => ({
	passage_id: `hit-${index}`,
	book_title: books[0].title,
	toc_path: ['Chapter 1', 'Newton’s laws'],
	content: `Original passage ${index + 1}: Force equals mass times acceleration.`,
	content_sha256: 'hash',
	matched_concepts: ['Force', 'Acceleration'],
	provenance,
	score: 0.9,
	excerpt: {
		content: 'Force equals mass times acceleration.',
		start_codepoint: 20,
		end_codepoint: 56
	}
});

try {
	browser = await chromium.launch({
		headless: true,
		...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
			? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
			: {})
	});
	const base = `http://127.0.0.1:${server.httpServer.address().port}/__epub_test`;
	await fs.mkdir('.tmp/epub-ui', { recursive: true });
	for (const width of [320, 390, 1280]) {
		const page = await browser.newPage({ viewport: { width, height: 900 } });
		const errors = [];
		page.on('pageerror', (error) => errors.push(error.message));
		const calls = [];
		let failList = false;
		let failPassages = false;
		let failSearch = false;
		let empty = false;
		let slowBook = false;
		let slowSearch = false;
		await page.route('**/users/reader/profile/image', (route) =>
			route.fulfill({
				contentType: 'image/png',
				body: Buffer.from(
					'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jK9kAAAAASUVORK5CYII=',
					'base64'
				)
			})
		);
		await page.route('**/api/v1/epub/**', async (route) => {
			const url = new URL(route.request().url());
			calls.push({ url: url.pathname + url.search, body: route.request().postDataJSON() });
			if (url.pathname.endsWith('/books')) {
				await route.fulfill(
					failList
						? { status: 500, json: { detail: 'Books unavailable' } }
						: { json: empty ? [] : books }
				);
			} else if (url.pathname.includes('/books/')) {
				const book = books.find((item) => url.pathname.endsWith(item.book_id));
				if (slowBook && book.book_id === 'physics')
					await new Promise((resolve) => setTimeout(resolve, 350));
				await route.fulfill({
					json: {
						...book,
						versions: [
							{ version_id: `${book.book_id}-current`, status: book.current_version_status },
							...(book.book_id === 'physics'
								? [{ version_id: 'physics-previous', status: 'ARCHIVED' }]
								: [])
						]
					}
				});
			} else if (url.pathname.endsWith('/passages')) {
				const version = url.pathname.split('/').at(-2);
				const offset = Number(url.searchParams.get('offset'));
				await route.fulfill(
					failPassages
						? { status: 500, json: { detail: 'Passages unavailable' } }
						: {
								json: {
									version_id: version,
									total: 75,
									offset,
									items: Array.from({ length: Math.min(50, 75 - offset) }, (_, i) =>
										passage(version, i + offset)
									)
								}
							}
				);
			} else if (url.pathname.endsWith('/search')) {
				if (slowSearch) await new Promise((resolve) => setTimeout(resolve, 350));
				const payload = route.request().postDataJSON();
				await route.fulfill(
					failSearch
						? { status: 500, json: { detail: 'Search unavailable' } }
						: {
								json: {
									query: payload.query,
									resolved_concepts: ['Force', 'Acceleration'],
									graph_total: 25,
									graph_offset: payload.graph_offset,
									graph_results: Array.from({ length: payload.graph_offset ? 5 : 20 }, (_, i) =>
										hit(i + payload.graph_offset, ['graph'])
									),
									vector_results: [hit(99, ['vector'])],
									fused_results: [hit(0, ['graph', 'vector']), hit(1, ['graph'])],
									degraded: [{ component: 'vector', reason: 'Partial index' }]
								}
							}
				);
			}
		});
		const chooseBook = async (id) => {
			if (width < 768)
				await page
					.getByRole('combobox', { name: 'Shared bookshelf', exact: true })
					.selectOption(id);
			else
				await page
					.getByRole('button', {
						name: new RegExp(books.find((book) => book.book_id === id).title)
					})
					.click();
		};
		await page.goto(base);
		await page.getByRole('heading', { name: books[0].title, exact: true }).waitFor();
		await page.getByText('1–50 / 75', { exact: true }).waitFor();
		await page.evaluate(() => document.fonts.ready);
		await page.screenshot({ path: `.tmp/epub-ui/${width}-reading.png` });
		await page.getByRole('button', { name: 'Next page', exact: true }).click();
		await page.getByText('51–75 / 75', { exact: true }).waitFor();
		await page.getByRole('button', { name: 'Previous page', exact: true }).click();
		await page.getByText('1–50 / 75', { exact: true }).waitFor();
		await page.getByLabel('Version', { exact: true }).selectOption('physics-previous');
		await page.waitForFunction(() =>
			document.querySelectorAll('article')[0]?.textContent.includes('Passage 1')
		);
		assert.ok(calls.some((call) => call.url.includes('/physics-previous/passages')));
		if (width >= 768) {
			await page.getByRole('searchbox', { name: 'Filter books' }).fill('geometry');
			assert.equal(
				await page.getByRole('button', { name: /Physics and Natural Science/ }).count(),
				0
			);
			await page.getByRole('searchbox', { name: 'Filter books' }).fill('');
		}
		await chooseBook('pending');
		await page.getByText('Book is being processed', { exact: true }).waitFor();
		assert.ok(!calls.some((call) => call.url.includes('/pending-current/passages')));
		slowBook = true;
		await chooseBook('physics');
		await chooseBook('geometry');
		await page.getByRole('heading', { name: books[1].title, exact: true }).waitFor();
		await page.waitForTimeout(450);
		assert.equal(await page.getByRole('heading', { name: books[0].title, exact: true }).count(), 0);
		slowBook = false;
		await page.getByRole('searchbox', { name: 'Search concepts or terms' }).fill('force');
		await page.getByRole('button', { name: 'Search', exact: true }).click();
		await page.getByRole('heading', { name: 'force', exact: true }).waitFor();
		assert.equal(await page.locator('article').count(), 2);
		await page.screenshot({ path: `.tmp/epub-ui/${width}-search.png` });
		await page.getByRole('button', { name: /Graph matches/ }).click();
		assert.equal(await page.locator('article').count(), 20);
		await page.getByRole('searchbox', { name: 'Search concepts or terms' }).fill('unsent edit');
		await page.getByRole('button', { name: 'Next page', exact: true }).click();
		await page.getByText('21–25 / 25', { exact: true }).waitFor();
		assert.equal(calls.filter((call) => call.body?.query).at(-1).body.query, 'force');
		await page.getByRole('button', { name: 'Previous page', exact: true }).click();
		await page.getByText('1–20 / 25', { exact: true }).waitFor();
		await page.getByRole('button', { name: /Semantic matches/ }).click();
		assert.equal(await page.locator('article').count(), 1);
		await page.getByRole('button', { name: 'Back to reading', exact: true }).click();
		await page.getByRole('heading', { name: books[1].title, exact: true }).waitFor();
		failSearch = true;
		await page.getByRole('searchbox', { name: 'Search concepts or terms' }).fill('force');
		await page.getByRole('button', { name: 'Search', exact: true }).click();
		await page.getByRole('alert').filter({ hasText: 'Search unavailable' }).waitFor();
		failSearch = false;
		await page.getByRole('button', { name: 'Retry', exact: true }).click();
		await page.getByRole('heading', { name: 'force', exact: true }).waitFor();
		await page.getByRole('button', { name: 'Clear search', exact: true }).click();
		slowSearch = true;
		await page.getByRole('searchbox', { name: 'Search concepts or terms' }).fill('late response');
		await page.getByRole('button', { name: 'Search', exact: true }).click();
		await page.getByRole('button', { name: 'Clear search', exact: true }).click();
		await page.waitForTimeout(450);
		assert.equal(
			await page.getByRole('heading', { name: 'late response', exact: true }).count(),
			0
		);
		slowSearch = false;
		failPassages = true;
		await chooseBook('physics');
		await page.getByRole('alert').filter({ hasText: 'Passages unavailable' }).waitFor();
		failPassages = false;
		await page.getByRole('button', { name: 'Retry', exact: true }).click();
		await page.getByText('1–50 / 75', { exact: true }).waitFor();
		for (const language of ['zh-CN', 'zh-TW', 'es-ES']) {
			await page.goto(`${base}?lang=${language}&theme=dark&sidebar=${width >= 768 ? '1' : '0'}`);
			await page.getByRole('heading', { name: books[0].title, exact: true }).waitFor();
			await page.getByText('1–50 / 75', { exact: true }).waitFor();
			await page.screenshot({ path: `.tmp/epub-ui/${width}-${language}-dark.png` });
			assert.ok(
				await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
				`${language}: horizontal overflow`
			);
			assert.ok(
				await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 1),
				`${language}: page should not scroll`
			);
			await page.getByRole('searchbox').first().fill('force');
			await page.getByRole('button', { name: /^(搜索|搜尋|Buscar)$/ }).click();
			await page.getByRole('heading', { name: 'force', exact: true }).waitFor();
			await page.screenshot({ path: `.tmp/epub-ui/${width}-${language}-search-dark.png` });
			assert.ok(
				await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1),
				`${language}: search horizontal overflow`
			);
		}
		failList = true;
		await page.goto(base);
		await page.getByRole('alert').filter({ hasText: 'Books unavailable' }).waitFor();
		failList = false;
		await page.getByRole('button', { name: 'Retry', exact: true }).click();
		await page.getByRole('heading', { name: books[0].title, exact: true }).waitFor();
		empty = true;
		await page.goto(`${base}?role=user`);
		await page.getByText('No books yet', { exact: true }).last().waitFor();
		assert.equal(await page.getByRole('link', { name: 'Manage library', exact: true }).count(), 0);
		assert.deepEqual(errors, []);
		await page.close();
	}
	console.log(
		'EPUB UI passed: reading, versions, pagination, book filtering, request races, search channels, retry, empty state, permissions, four languages and desktop/mobile themes.'
	);
} finally {
	await browser?.close();
	await server.close();
}
