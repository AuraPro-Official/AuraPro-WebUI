import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const read = (name) => JSON.parse(readFileSync(new URL(`../${name}`, import.meta.url), 'utf8'));
const manifest = read('package.json');
const lock = read('package-lock.json');
const floors = {
	'@sveltejs/kit': '2.70.4',
	'@sveltejs/adapter-static': '3.0.11',
	minimatch: '11.0.1',
	glob: '14.0.2',
	systeminformation: '5.33.16',
	'prosemirror-view': '1.42.3',
	'@tiptap/core': '3.30.5',
	dompurify: '3.4.17',
	katex: '0.18.2',
	mermaid: '11.16.1',
	undici: '7.30.1',
	sharp: '0.35.5',
	'brace-expansion': '5.0.12',
	'postcss-selector-parser': '7.1.6',
	vite: '7.3.7',
	vitest: '4.1.12'
};
const atLeast = (version, minimum) => {
	const a = version.split('.').map(Number);
	const b = minimum.split('.').map(Number);
	for (let i = 0; i < 3; i++) {
		if (a[i] !== b[i]) return a[i] > b[i];
	}
	return true;
};

test('every locked copy of security-sensitive dependencies meets its floor', () => {
	for (const [name, minimum] of Object.entries(floors)) {
		const copies = Object.entries(lock.packages).filter(([path]) =>
			path.endsWith(`node_modules/${name}`)
		);
		assert.ok(copies.length, `${name} must remain covered`);
		for (const [path, pkg] of copies) {
			if (name === 'glob' && Number(pkg.version.split('.')[0]) < 9) continue;
			if (name === 'minimatch' && Number(pkg.version.split('.')[0]) < 7) continue;
			assert.ok(atLeast(pkg.version, minimum), `${path}@${pkg.version} is below ${minimum}`);
		}
	}
});

test('manifest and lock root agree and dedicated terminal packages are absent', () => {
	for (const kind of ['dependencies', 'devDependencies']) {
		assert.deepEqual(lock.packages[''][kind], manifest[kind]);
		assert.ok(!Object.keys(manifest[kind]).some((name) => name.startsWith('@xterm/')));
	}
	assert.ok(!Object.keys(lock.packages).some((path) => /node_modules\/@xterm\//.test(path)));
	const tiptap = Object.entries(manifest.dependencies).filter(
		([name]) => name.startsWith('@tiptap/') && name !== '@tiptap/y-tiptap'
	);
	assert.equal(new Set(tiptap.map(([, version]) => version)).size, 1);
});
