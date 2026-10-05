<script lang="ts">
	import { configurePiModel, checkPiExtensions } from '$lib/apis/opencode';
	export let directory = '';
	export let onConfigured: (model: string) => void | Promise<void> = () => {};
	let expanded = false;
	let baseUrl = 'http://127.0.0.1:18881/v1';
	let model = '';
	let apiKey = '';
	let api = 'openai-completions';
	let vision = false;
	let contextWindow = 32768;
	let busy = false;
	let error = '';
	let checks: { tool: string; available?: boolean; error?: string; detail?: string }[] = [];
	let commands: string[] = [];
	async function save() {
		busy = true;
		error = '';
		try {
			const result = await configurePiModel(localStorage.token, {
				base_url: baseUrl,
				model,
				api_key: apiKey,
				api,
				vision,
				context_window: contextWindow
			});
			apiKey = '';
			await onConfigured(result.model);
		} catch (cause) {
			error = String(cause);
		} finally {
			busy = false;
		}
	}
	async function check() {
		busy = true;
		error = '';
		try {
			const result = await checkPiExtensions(localStorage.token, directory);
			checks = result.checks;
			commands = result.commands;
		} catch (cause) {
			error = String(cause);
		} finally {
			busy = false;
		}
	}
</script>

<div class="space-y-2 text-xs">
	<button
		type="button"
		class="text-blue-600 dark:text-blue-400"
		on:click={() => (expanded = !expanded)}>PI · 模型与扩展设置</button
	>
	{#if expanded}
		<div class="space-y-2 rounded-lg border border-gray-200 p-2 dark:border-gray-700">
			<label class="block"
				>API 地址<input
					class="mt-1 w-full rounded border bg-transparent p-1"
					bind:value={baseUrl}
				/></label
			>
			<label class="block"
				>模型 ID<input
					class="mt-1 w-full rounded border bg-transparent p-1"
					bind:value={model}
				/></label
			>
			<label class="block"
				>API 协议<select
					class="mt-1 w-full rounded border bg-white p-1 dark:bg-gray-900"
					bind:value={api}
					><option value="openai-completions">OpenAI Compatible</option><option
						value="openai-responses">OpenAI Responses</option
					><option value="anthropic-messages">Anthropic</option><option value="google-generative-ai"
						>Google</option
					></select
				></label
			>
			<label class="block"
				>API Key（本地服务可留空）<input
					type="password"
					autocomplete="off"
					class="mt-1 w-full rounded border bg-transparent p-1"
					bind:value={apiKey}
				/></label
			>
			<label class="block"
				>上下文长度<input
					type="number"
					min="4096"
					max="2000000"
					class="mt-1 w-full rounded border bg-transparent p-1"
					bind:value={contextWindow}
				/></label
			>
			<label class="flex gap-2"><input type="checkbox" bind:checked={vision} />模型支持图片</label>
			<button
				type="button"
				class="rounded bg-blue-600 px-2 py-1 text-white disabled:opacity-40"
				disabled={busy || !model.trim()}
				on:click={save}>保存并选择模型</button
			>
			<div class="border-t pt-2 dark:border-gray-700">
				<div>扩展在桌面端 PI 设置中安装。此处检查浏览器连接与桌面访问。</div>
				<button
					type="button"
					class="mt-2 rounded border px-2 py-1 disabled:opacity-40"
					disabled={busy || !directory}
					on:click={check}>{busy ? '正在检查…' : '检查扩展功能'}</button
				>
				{#each checks as item}<div class="mt-2">
						<strong>{item.tool} · {item.available ? '检查通过' : '待配置'}</strong>
						<div class="whitespace-pre-wrap break-all text-gray-500">
							{item.error || item.detail}
						</div>
					</div>{/each}
				{#if commands.length}<div class="mt-2 break-all">
						聊天中可用命令：{commands.map((name) => `/${name}`).join('、')}
					</div>{/if}
			</div>
			{#if error}<div class="break-all text-red-600">{error}</div>{/if}
		</div>
	{/if}
</div>
