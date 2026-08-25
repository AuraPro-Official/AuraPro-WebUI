<script>
	import { getContext } from 'svelte';
	const i18n = getContext('i18n');
	import Search from '$lib/components/icons/Search.svelte';

	export let status = null;
	export let done = false;

	const formatDuration = (value) => {
		const seconds = Math.max(0, Number(value) || 0);
		const hours = Math.floor(seconds / 3600);
		const minutes = Math.floor((seconds % 3600) / 60);
		const remainingSeconds = Math.floor(seconds % 60);
		return hours > 0
			? `${hours}:${String(minutes).padStart(2, '0')}:${String(remainingSeconds).padStart(2, '0')}`
			: `${minutes}:${String(remainingSeconds).padStart(2, '0')}`;
	};

	const getOpenCodePhaseLabel = (phase) => {
		switch (phase) {
			case 'planning':
				return 'OpenCode is planning';
			case 'tool':
				return 'OpenCode is running a tool';
			case 'waiting':
				return 'Waiting for the current OpenCode step';
			case 'finishing':
				return 'OpenCode is preparing the result';
			case 'completed':
				return 'OpenCode task completed';
			default:
				return 'OpenCode is working';
		}
	};

	const getOpenCodeDetail = (value) => String(value ?? '').replace(/^OpenCode\s*·\s*/, '');
</script>

{#if !status?.hidden}
	<div class="status-description flex items-center gap-2 py-0.5 w-full text-left">
		{#if status?.action === 'opencode_progress'}
			<div class="flex min-w-0 flex-col justify-center gap-0.5">
				<div
					class="{(done || status?.done) === false
						? 'shimmer'
						: ''} text-base text-gray-500 dark:text-gray-500"
				>
					{$i18n.t(getOpenCodePhaseLabel(status?.phase))}
					<span class="ml-1 whitespace-nowrap font-normal tabular-nums">
						· {$i18n.t('Elapsed {{time}}', {
							time: formatDuration(status?.elapsed_seconds)
						})}
					</span>
				</div>
				{#if status?.detail}
					<div class="line-clamp-2 text-xs text-gray-500 dark:text-gray-400">
						{getOpenCodeDetail(status.detail)}
					</div>
				{/if}
				{#if status?.delayed}
					<div class="text-xs text-amber-700 dark:text-amber-400">
						{$i18n.t('This OpenCode step is taking longer than usual')}
						<span class="whitespace-nowrap">
							· {$i18n.t('No new activity for {{time}}', {
								time: formatDuration(status?.idle_seconds)
							})}
						</span>
					</div>
				{/if}
			</div>
		{:else if status?.action === 'knowledge_search'}
			<div class="flex flex-col justify-center -space-y-0.5">
				<div
					class="{(done || status?.done) === false
						? 'shimmer'
						: ''} text-gray-500 dark:text-gray-500 text-base line-clamp-1 text-wrap"
				>
					{$i18n.t(`Searching Knowledge for "{{searchQuery}}"`, {
						searchQuery: status.query
					})}
				</div>
			</div>
		{:else if status?.action === 'queries_generated' && status?.queries}
			<div class="flex flex-col justify-center -space-y-0.5">
				<div
					class="{(done || status?.done) === false
						? 'shimmer'
						: ''} text-gray-500 dark:text-gray-500 text-base line-clamp-1 text-wrap"
				>
					{$i18n.t(`Querying`)}
				</div>

				<div class=" flex gap-1 flex-wrap mt-2">
					{#each status.queries as query, idx (query)}
						<div
							class="bg-gray-50 dark:bg-gray-850 flex rounded-lg py-1 px-2 items-center gap-1 text-xs"
						>
							<div>
								<Search className="size-3" />
							</div>

							<span class="line-clamp-1">
								{query}
							</span>
						</div>
					{/each}
				</div>
			</div>
		{:else if status?.action === 'sources_retrieved' && status?.count !== undefined}
			<div class="flex flex-col justify-center -space-y-0.5">
				<div
					class="{(done || status?.done) === false
						? 'shimmer'
						: ''} text-gray-500 dark:text-gray-500 text-base line-clamp-1 text-wrap"
				>
					{#if status.count === 0}
						{$i18n.t('No sources found')}
					{:else if status.count === 1}
						{$i18n.t('Retrieved 1 source')}
					{:else}
						<!-- {$i18n.t('Source')} -->
						<!-- {$i18n.t('No source available')} -->
						<!-- {$i18n.t('No distance available')} -->
						<!-- {$i18n.t('Retrieved {{count}} sources')} -->
						{$i18n.t('Retrieved {{count}} sources', {
							count: status.count
						})}
					{/if}
				</div>
			</div>
		{:else}
			<div class="flex flex-col justify-center -space-y-0.5">
				<div
					class="{(done || status?.done) === false
						? 'shimmer'
						: ''} text-gray-500 dark:text-gray-500 text-base line-clamp-1 text-wrap"
				>
					<!-- $i18n.t(`Searching "{{searchQuery}}"`) -->
					{#if status?.description?.includes('{{searchQuery}}')}
						{$i18n.t(status?.description, {
							searchQuery: status?.query
						})}
					{:else if status?.description === 'No search query generated'}
						{$i18n.t('No search query generated')}
					{:else if status?.description === 'Generating search query'}
						{$i18n.t('Generating search query')}
					{:else if status?.description === 'Searching the web'}
						{$i18n.t('Searching the web')}
					{:else}
						{$i18n.t(status?.description ?? '')}
					{/if}
				</div>
			</div>
		{/if}
	</div>
{/if}
