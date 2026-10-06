<script lang="ts">
	import { getContext } from 'svelte';
	import { piToolLabel, piStatusLabel } from '$lib/utils/pi-language';
	const i18n = getContext('i18n');
	export let tools: {
		name: string;
		status: string;
		content: { type: string; text?: string; data?: string; mimeType?: string }[];
	}[] = [];
</script>

{#if tools.length}
	<details class="my-2 rounded-lg border border-gray-200 p-3 text-xs dark:border-gray-800">
		<summary class="cursor-pointer">PI · {$i18n.t('Tool results')} ({tools.length})</summary>
		{#each tools as tool}
			<div class="mt-3 border-t border-gray-100 pt-2 dark:border-gray-800">
				<strong>{piToolLabel(tool.name, $i18n)} · {piStatusLabel(tool.status, $i18n)}</strong>
				{#each tool.content as block}
					{#if block.type === 'text'}
						<pre
							class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all">{block.text}</pre>
					{:else if block.type === 'image' && block.data && ['image/png', 'image/jpeg', 'image/webp'].includes(block.mimeType ?? '')}
						<img
							class="mt-2 max-h-96 rounded object-contain"
							src={`data:${block.mimeType};base64,${block.data}`}
							alt={`${tool.name} 截图`}
						/>
					{/if}
				{/each}
			</div>
		{/each}
	</details>
{/if}
