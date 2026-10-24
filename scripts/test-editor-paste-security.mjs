import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import path from 'node:path';
import { createServer } from 'vite';

const require = createRequire(import.meta.url);
const { chromium } = require(
	require.resolve('playwright', {
		paths: [process.cwd(), path.join(path.dirname(process.execPath), '..', 'node_modules')]
	})
);
const server = await createServer({
	configFile: false,
	appType: 'custom',
	root: process.cwd(),
	server: { host: '127.0.0.1', port: 0 }
});
let browser;

server.middlewares.use('/__paste_security_test', async (_request, response) => {
	response.setHeader('Content-Type', 'text/html');
	response.end(
		await server.transformIndexHtml(
			'/__paste_security_test',
			`<!doctype html><html><body><div id="editor"></div><script type="module">
import { Schema } from 'prosemirror-model';
import { EditorState } from 'prosemirror-state';
import { EditorView, __parseFromClipboard } from 'prosemirror-view';

const schema = new Schema({nodes: {
  doc: {content: 'block+'},
  paragraph: {group: 'block', content: 'inline*', parseDOM: [{tag: 'p'}], toDOM: () => ['p', 0]},
  wrapper: {
    group: 'block', content: 'block+',
    attrs: {label: {default: '', validate: 'string'}},
    toDOM: node => ['section', {'data-label': node.attrs.label}, 0]
  },
  text: {group: 'inline'}
}});
const view = new EditorView(document.querySelector('#editor'), {state: EditorState.create({schema})});
const paste = label => {
  const element = document.createElement('p');
  element.textContent = 'Pasted text';
  element.setAttribute('data-pm-slice', '1 1 ' + JSON.stringify(['wrapper', {label}]));
  return __parseFromClipboard(view, '', element.outerHTML, false, view.state.doc.resolve(1));
};
const benign = paste('safe');
const invalid = paste(['invalid attribute type']);
const plain = __parseFromClipboard(view, 'Ordinary text', '', true, view.state.doc.resolve(1));
window.pasteResult = {
  benignWrapped: benign.content.firstChild.type.name === 'wrapper',
  benignLabel: benign.content.firstChild.attrs.label,
  invalidWrapped: invalid.content.firstChild.type.name === 'wrapper',
  invalidText: invalid.content.textBetween(0, invalid.content.size),
  plainText: plain.content.textBetween(0, plain.content.size)
};
view.destroy();
</script></body></html>`
		)
	);
});

try {
	await server.listen();
	browser = await chromium.launch({
		headless: true,
		channel: process.env.TEST_BROWSER || 'chrome'
	});
	const page = await browser.newPage();
	const errors = [];
	page.on('pageerror', (error) => errors.push(error.message));
	await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/__paste_security_test`);
	await page.waitForFunction(() => window.pasteResult);
	assert.deepEqual(errors, []);
	assert.deepEqual(await page.evaluate(() => window.pasteResult), {
		benignWrapped: true,
		benignLabel: 'safe',
		invalidWrapped: false,
		invalidText: 'Pasted text',
		plainText: 'Ordinary text'
	});
	console.log(
		'Clipboard regression passed: invalid slice attributes rejected; valid and plain text preserved.'
	);
} finally {
	await browser?.close();
	await server.close();
}
