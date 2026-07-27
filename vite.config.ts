import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import { existsSync, readdirSync } from 'node:fs';

import { viteStaticCopy } from 'vite-plugin-static-copy';

const onnxRuntimeDist = 'node_modules/onnxruntime-web/dist';
const onnxRuntimeJsepTargets =
	existsSync(onnxRuntimeDist) &&
	readdirSync(onnxRuntimeDist).some((file) => file.includes('.jsep.'))
		? [
				{
					src: `${onnxRuntimeDist}/*.jsep.*`,
					dest: 'wasm'
				}
			]
		: [];

export default defineConfig(({ mode }) => ({
	plugins: [
		sveltekit(),
		...(onnxRuntimeJsepTargets.length > 0
			? [
					viteStaticCopy({
						targets: onnxRuntimeJsepTargets
					})
				]
			: [])
	],
	define: {
		APP_VERSION: JSON.stringify(process.env.npm_package_version),
		APP_BUILD_HASH: JSON.stringify(process.env.APP_BUILD_HASH || 'dev-build')
	},
	build: {
		sourcemap: process.env.GENERATE_SOURCEMAP === 'true'
	},
	worker: {
		format: 'es'
	},
	esbuild: {
		pure: mode === 'development' ? [] : ['console.log', 'console.debug', 'console.info']
	}
}));
