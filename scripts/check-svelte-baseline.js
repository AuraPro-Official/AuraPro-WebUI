import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const require = createRequire(import.meta.url);
const root = process.cwd();
const baselinePath = path.join(root, 'scripts', 'svelte-check-baseline.json');
const update = process.argv.includes('--update');

const resolveBin = (packageName) => {
	const packagePath = require.resolve(`${packageName}/package.json`);
	const packageJson = JSON.parse(fs.readFileSync(packagePath, 'utf8'));
	const binPath =
		typeof packageJson.bin === 'string'
			? packageJson.bin
			: packageJson.bin[Object.keys(packageJson.bin)[0]];
	return path.resolve(path.dirname(packagePath), binPath);
};

const runNodeBin = (packageName, args) =>
	spawnSync(process.execPath, [resolveBin(packageName), ...args], {
		cwd: root,
		encoding: 'utf8',
		maxBuffer: 128 * 1024 * 1024
	});

const sync = runNodeBin('@sveltejs/kit', ['sync']);
if (sync.status !== 0) {
	process.stderr.write(sync.stdout ?? '');
	process.stderr.write(sync.stderr ?? '');
	process.exit(sync.status ?? 1);
}

const check = runNodeBin('svelte-check', [
	'--tsconfig',
	'./tsconfig.json',
	'--output',
	'machine',
	'--threshold',
	'error',
	'--no-color'
]);
const output = `${check.stdout ?? ''}\n${check.stderr ?? ''}`;
const linePattern = /^\d+\s+ERROR\s+("(?:\\.|[^"\\])*")\s+(\d+):(\d+)\s+("(?:\\.|[^"\\])*")$/;
const diagnostics = [];

for (const line of output.split(/\r?\n/)) {
	const match = line.match(linePattern);
	if (!match) continue;

	diagnostics.push({
		file: JSON.parse(match[1]).replaceAll('\\', '/'),
		line: Number(match[2]),
		column: Number(match[3]),
		message: JSON.parse(match[4])
	});
}

const operationalErrors = diagnostics.filter((diagnostic) =>
	/^(EACCES|EPERM):/.test(diagnostic.message)
);
if (operationalErrors.length > 0) {
	for (const diagnostic of operationalErrors) {
		console.error(
			`Type checker could not read ${diagnostic.file}:${diagnostic.line}: ${diagnostic.message}`
		);
	}
	process.exit(1);
}

if (check.status !== 0 && diagnostics.length === 0) {
	process.stderr.write(output);
	console.error('svelte-check failed without machine-readable diagnostics.');
	process.exit(check.status ?? 1);
}

const grouped = new Map();
for (const diagnostic of diagnostics) {
	const key = `${diagnostic.file}\0${diagnostic.message}`;
	const group = grouped.get(key) ?? {
		file: diagnostic.file,
		message: diagnostic.message,
		count: 0,
		lines: new Set()
	};
	group.count += 1;
	group.lines.add(diagnostic.line);
	grouped.set(key, group);
}

const groups = [...grouped.values()]
	.map((group) => ({
		file: group.file,
		message: group.message,
		count: group.count,
		lines: [...group.lines].sort((a, b) => a - b)
	}))
	.sort((a, b) => a.file.localeCompare(b.file) || a.message.localeCompare(b.message));

const baseline = {
	schemaVersion: 1,
	totalErrors: diagnostics.length,
	groups
};

if (update) {
	fs.writeFileSync(baselinePath, `${JSON.stringify(baseline, null, 2)}\n`);
	console.log(`Updated Svelte baseline: ${diagnostics.length} errors in ${groups.length} groups.`);
	process.exit(0);
}

if (!fs.existsSync(baselinePath)) {
	console.error(
		'Svelte baseline is missing. Run npm run check:update-baseline after reviewing errors.'
	);
	process.exit(1);
}

const expected = JSON.parse(fs.readFileSync(baselinePath, 'utf8'));
const expectedCounts = new Map(
	expected.groups.map((group) => [`${group.file}\0${group.message}`, group.count])
);
const actualCounts = new Map(
	groups.map((group) => [`${group.file}\0${group.message}`, group.count])
);
const changes = [];

for (const [key, count] of actualCounts) {
	const expectedCount = expectedCounts.get(key) ?? 0;
	if (count !== expectedCount) {
		const group = grouped.get(key);
		changes.push({
			kind: count > expectedCount ? 'new' : 'fixed',
			file: group.file,
			message: group.message,
			expected: expectedCount,
			actual: count
		});
	}
}

for (const [key, expectedCount] of expectedCounts) {
	if (!actualCounts.has(key)) {
		const separator = key.indexOf('\0');
		changes.push({
			kind: 'fixed',
			file: key.slice(0, separator),
			message: key.slice(separator + 1),
			expected: expectedCount,
			actual: 0
		});
	}
}

if (changes.length > 0) {
	for (const change of changes.slice(0, 30)) {
		console.error(
			`${change.kind === 'new' ? 'NEW' : 'FIXED'} ${change.file}: ${change.expected} -> ${change.actual} ${change.message}`
		);
	}
	if (changes.length > 30) {
		console.error(`...and ${changes.length - 30} more baseline changes.`);
	}
	console.error('Review the changes, then run npm run check:update-baseline.');
	process.exit(1);
}

console.log(`Svelte baseline unchanged: ${diagnostics.length} errors in ${groups.length} groups.`);
