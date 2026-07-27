const packages = [
	'micropip',
	'packaging',
	'requests',
	'beautifulsoup4',
	'numpy',
	'pandas',
	'matplotlib',
	'scikit-learn',
	'scipy',
	'regex',
	'sympy',
	'tiktoken',
	'seaborn',
	'pytz',
	'black',
	'openai',
	'openpyxl'
];

// Pure-Python packages whose wheels must be downloaded from PyPI and saved into
// static/pyodide/ so that the browser can install them offline via micropip.
// Packages already provided by the Pyodide distribution (click, platformdirs,
// typing_extensions, etc.) do NOT need to be listed here.
const pypiPackages = [
	{ name: 'black', version: '26.5.1' },
	{ name: 'pathspec', version: '1.1.1' },
	{ name: 'mypy_extensions', version: '1.1.0' },
	{ name: 'pytokens', version: '0.4.1' }
];

import { loadPyodide } from 'pyodide';
import { setGlobalDispatcher, ProxyAgent } from 'undici';
import { writeFile, readFile, copyFile, readdir, rm, mkdir, access } from 'fs/promises';

const CACHE_MANIFEST_PATH = 'static/pyodide/aurapro-cache.json';
const REQUIRED_RUNTIME_FILES = [
	'package.json',
	'pyodide.js',
	'pyodide.asm.wasm',
	'pyodide-lock.json'
];

/**
 * Loading network proxy configurations from the environment variables.
 * And the proxy config with lowercase name has the highest priority to use.
 */
function initNetworkProxyFromEnv() {
	// we assume all subsequent requests in this script are HTTPS:
	// https://cdn.jsdelivr.net
	// https://pypi.org
	// https://files.pythonhosted.org
	const allProxy = process.env.all_proxy || process.env.ALL_PROXY;
	const httpsProxy = process.env.https_proxy || process.env.HTTPS_PROXY;
	const httpProxy = process.env.http_proxy || process.env.HTTP_PROXY;
	const preferedProxy = httpsProxy || allProxy || httpProxy;
	/**
	 * use only http(s) proxy because socks5 proxy is not supported currently:
	 * @see https://github.com/nodejs/undici/issues/2224
	 */
	if (!preferedProxy || !preferedProxy.startsWith('http')) return;
	let preferedProxyURL;
	try {
		preferedProxyURL = new URL(preferedProxy).toString();
	} catch {
		console.warn(`Invalid network proxy URL: "${preferedProxy}"`);
		return;
	}
	const dispatcher = new ProxyAgent({ uri: preferedProxyURL });
	setGlobalDispatcher(dispatcher);
	console.log(`Initialized network proxy "${preferedProxy}" from env`);
}

async function downloadPackages() {
	console.log('Setting up pyodide + micropip');

	let pyodide;
	try {
		pyodide = await loadPyodide({
			packageCacheDir: 'static/pyodide'
		});
	} catch (err) {
		console.error('Failed to load Pyodide:', err);
		throw err;
	}

	try {
		console.log('Loading micropip package');
		await pyodide.loadPackage('micropip');

		const micropip = pyodide.pyimport('micropip');
		console.log('Downloading Pyodide packages:', packages);

		try {
			for (const pkg of packages) {
				console.log(`Installing package: ${pkg}`);
				await micropip.install(pkg);
			}
		} catch (err) {
			console.error('Package installation failed:', err);
			throw err;
		}

		console.log('Pyodide packages downloaded, freezing into lock file');

		try {
			const lockFile = await micropip.freeze();
			await writeFile('static/pyodide/pyodide-lock.json', lockFile);
		} catch (err) {
			console.error('Failed to write lock file:', err);
			throw err;
		}
	} catch (err) {
		console.error('Failed to load or install micropip:', err);
		throw err;
	}
}

async function copyPyodide() {
	console.log('Copying Pyodide files into static directory');
	// Copy all files from node_modules/pyodide to static/pyodide
	for await (const entry of await readdir('node_modules/pyodide')) {
		await copyFile(`node_modules/pyodide/${entry}`, `static/pyodide/${entry}`);
	}
}

/**
 * Download pure-Python wheels from PyPI and save them into static/pyodide/.
 * Also injects entries into pyodide-lock.json so that micropip resolves these
 * packages from the local server instead of fetching them from the internet.
 */
async function downloadPyPIWheels() {
	const lockPath = 'static/pyodide/pyodide-lock.json';
	let lockData;
	try {
		lockData = JSON.parse(await readFile(lockPath, 'utf-8'));
	} catch (error) {
		throw new Error('Could not read pyodide-lock.json', { cause: error });
	}

	const wheelFiles = [];
	for (const { name: pkg, version: pinnedVersion } of pypiPackages) {
		console.log(`Fetching PyPI metadata for: ${pkg}==${pinnedVersion}`);
		const res = await fetch(`https://pypi.org/pypi/${pkg}/${pinnedVersion}/json`);
		if (!res.ok) {
			throw new Error(`Failed to fetch PyPI metadata for ${pkg}==${pinnedVersion}: ${res.status}`);
		}
		const meta = await res.json();
		const version = meta.info.version;
		const files = meta.urls || [];
		// Find the pure-Python wheel (py3-none-any)
		const wheel = files.find(
			(f) => f.filename.endsWith('.whl') && f.filename.includes('py3-none-any')
		);
		if (!wheel) {
			throw new Error(`No pure-Python wheel found for ${pkg}==${version}`);
		}
		const dest = `static/pyodide/${wheel.filename}`;
		wheelFiles.push(wheel.filename);
		// Download wheel if not already present
		try {
			await access(dest);
			console.log(`  Already exists: ${wheel.filename}`);
		} catch {
			console.log(`  Downloading: ${wheel.filename}`);
			const wheelRes = await fetch(wheel.url);
			if (!wheelRes.ok) {
				throw new Error(`Failed to download ${wheel.filename}: ${wheelRes.status}`);
			}
			const buffer = Buffer.from(await wheelRes.arrayBuffer());
			await writeFile(dest, buffer);
			console.log(`  Saved: ${dest} (${buffer.length} bytes)`);
		}

		// Inject into pyodide-lock.json so micropip resolves locally
		const normalizedName = pkg.replace(/-/g, '_');
		if (!lockData.packages[normalizedName]) {
			lockData.packages[normalizedName] = {
				name: normalizedName,
				version: version,
				file_name: wheel.filename,
				install_dir: 'site',
				sha256: wheel.digests?.sha256 || '',
				package_type: 'package',
				imports: [normalizedName],
				depends: []
			};
			console.log(`  Added ${normalizedName}==${version} to pyodide-lock.json`);
		}
	}

	await writeFile(lockPath, JSON.stringify(lockData, null, 2));
	console.log('Updated pyodide-lock.json with PyPI packages');
	return wheelFiles;
}

async function getCacheConfig() {
	const packageJson = JSON.parse(await readFile('node_modules/pyodide/package.json', 'utf8'));
	return {
		schemaVersion: 1,
		pyodideVersion: packageJson.version,
		packages,
		pypiPackages
	};
}

async function isCacheValid(cacheConfig) {
	try {
		const manifest = JSON.parse(await readFile(CACHE_MANIFEST_PATH, 'utf8'));
		const { files, ...savedConfig } = manifest;
		if (JSON.stringify(savedConfig) !== JSON.stringify(cacheConfig) || !Array.isArray(files)) {
			return false;
		}

		await Promise.all(files.map((file) => access(`static/pyodide/${file}`)));
		return true;
	} catch {
		return false;
	}
}

initNetworkProxyFromEnv();
const cacheConfig = await getCacheConfig();

if (await isCacheValid(cacheConfig)) {
	console.log(`Using cached Pyodide ${cacheConfig.pyodideVersion} runtime`);
} else {
	console.log(`Preparing Pyodide ${cacheConfig.pyodideVersion} runtime`);
	await rm('static/pyodide', { recursive: true, force: true });
	await mkdir('static/pyodide', { recursive: true });
	await downloadPackages();
	await copyPyodide();
	const wheelFiles = await downloadPyPIWheels();
	await writeFile(
		CACHE_MANIFEST_PATH,
		JSON.stringify(
			{
				...cacheConfig,
				files: [...REQUIRED_RUNTIME_FILES, ...wheelFiles]
			},
			null,
			2
		)
	);
}
