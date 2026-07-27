<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { toast } from 'svelte-sonner';
	import { LANG_LABELS } from '$lib/apis/knowledge';
	import { getBilingualFiles, deleteBilingualFile } from '$lib/apis/retrieval';
	import Pagination from '$lib/components/common/Pagination.svelte';

	const i18n = getContext('i18n');

	export let knowledge: any = null;

	let groups: {
		base: string;
		bilingualId: string;
		languages: string[];
		primaryLang: string;
		sentenceCount: number;
	}[] = [];
	let loading = true;
	let currentPage = 1;
	let pageSize = 20;
	let totalCount = 0;
	let loadingTimeout: ReturnType<typeof setTimeout> | null = null;

	const getLangLabel = (code: string) =>
		LANG_LABELS[code?.toLowerCase()] ?? code?.toUpperCase() ?? '?';

	const loadFiles = async () => {
		if (!knowledge?.id) {
			groups = [];
			totalCount = 0;
			loading = false;
			return;
		}
		
		loading = true;
		
		// 设置加载超时，防止等待过长
		if (loadingTimeout) clearTimeout(loadingTimeout);
		loadingTimeout = setTimeout(() => {
			if (loading) {
				toast.warning($i18n?.t('Data loading is taking longer than expected') ?? '数据加载耗时较长');
			}
		}, 8000);

		try {
			const skip = (currentPage - 1) * pageSize;
			const res = await getBilingualFiles(localStorage.token, knowledge.id, skip, pageSize);
			if (res) {
				groups = (res?.files ?? []).map((f: any) => ({
					base: f.base_name,
					bilingualId: f.bilingual_id,
					languages: f.languages ?? [],
					primaryLang: f.primary_lang,
					sentenceCount: f.sentence_count
				}));
				totalCount = res?.total ?? 0;
			} else {
				groups = [];
				totalCount = 0;
			}
		} catch (e) {
			console.error(e);
			toast.error($i18n?.t('Failed to load bilingual files') ?? '加载双语文件失败');
			groups = [];
			totalCount = 0;
		} finally {
			loading = false;
			if (loadingTimeout) clearTimeout(loadingTimeout);
		}
	};

	const openAlignReview = (group: (typeof groups)[number]) => {
		goto(`/workspace/knowledge/bilingual-align/${group.bilingualId}?collection=${knowledge?.id ?? ''}`);
	};

	const handleDelete = async (group: (typeof groups)[number]) => {
		try {
			await deleteBilingualFile(localStorage.token, knowledge.id, group.bilingualId);
			groups = groups.filter((g) => g.bilingualId !== group.bilingualId);
			totalCount = Math.max(0, totalCount - 1);
			
			// 如果删除后当前页没有数据了，回到上一页
			if (groups.length === 0 && currentPage > 1) {
				currentPage = currentPage - 1;
				await loadFiles();
			}
			
			toast.success($i18n?.t('Deleted successfully') ?? '删除成功');
		} catch (e) {
			console.error(e);
			toast.error($i18n?.t('Failed to delete') ?? '删除失败');
		}
	};

	const handlePageChange = (page: number) => {
		currentPage = page;
	};

	onMount(() => {
		loadFiles();
		
		return () => {
			if (loadingTimeout) clearTimeout(loadingTimeout);
		};
	});
	
	// 监听页码变化并重新加载
	$: currentPage, (currentPage > 0) && loadFiles();
	
	// 监听知识库ID变化
	$: knowledge?.id, (currentPage = 1);
</script>

{#if loading}
	<div class="flex flex-col items-center justify-center py-10 gap-3">
		<div class="w-8 h-8 border-4 border-gray-200 dark:border-gray-700 border-t-blue-500 rounded-full animate-spin" />
		<p class="text-gray-400 text-sm">{$i18n?.t('Loading...') ?? '加载中...'}</p>
	</div>
{:else if groups.length === 0}
	<div class="flex flex-col items-center justify-center py-10 text-gray-400 text-sm">
		{$i18n?.t('No bilingual files found') ?? 'No bilingual files found'}
	</div>
{:else}
	<div class="flex flex-col w-full">
		<div class="w-full overflow-x-auto">
			<table class="w-full text-sm border-collapse">
				<thead>
					<tr class="border-b border-gray-100 dark:border-gray-800">
						<th class="text-left px-4 py-3 text-xs font-medium text-gray-400 dark:text-gray-500">
							{$i18n?.t('File Name') ?? 'File Name'}
						</th>
						<th class="text-left px-4 py-3 text-xs font-medium text-gray-400 dark:text-gray-500 w-40">
							{$i18n?.t('Alignment') ?? 'Alignment'}
						</th>
						<th class="text-left px-4 py-3 text-xs font-medium text-gray-400 dark:text-gray-500">
							{$i18n?.t('Sentences') ?? 'Sentences'}
						</th>
						<th class="text-left px-4 py-3 text-xs font-medium text-gray-400 dark:text-gray-500">
							{$i18n?.t('Languages') ?? 'Languages'}
						</th>
						{#if knowledge?.write_access}
							<th class="w-12" />
						{/if}
					</tr>
				</thead>

				<tbody class="divide-y divide-gray-50 dark:divide-gray-800/50">
					{#each groups as group}
						<tr class="hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors group align-top">
							<td class="px-4 py-4 text-xs font-medium text-gray-700 dark:text-gray-300">
								<div class="line-clamp-2 max-w-[280px]">
									{group.base}
								</div>
							</td>

							<td class="px-4 py-4">
								<button
									class="px-4 py-1.5 rounded-lg text-xs font-medium bg-blue-50 text-blue-600 hover:bg-blue-100
										dark:bg-blue-900/30 dark:text-blue-300 dark:hover:bg-blue-900/50 transition whitespace-nowrap"
									on:click={() => openAlignReview(group)}
								>
									{$i18n?.t('View Alignment') ?? '查看对齐'}
								</button>
							</td>

							<td class="px-4 py-4 text-xs text-gray-600 dark:text-gray-400">
								{group.sentenceCount}
							</td>

							<td class="px-4 py-4">
								<div class="flex items-center gap-1.5 flex-wrap">
									{#each group.languages as lang}
										<span
											class="inline-flex items-center px-2.5 py-0.5 rounded-md text-[11px] font-medium
												bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400"
											title={getLangLabel(lang)}
										>
											{lang.toUpperCase()}
										</span>
									{/each}
								</div>
							</td>

							{#if knowledge?.write_access}
								<td class="px-4 py-4">
									<button
										class="opacity-0 group-hover:opacity-100 transition p-2 rounded hover:bg-red-50
											dark:hover:bg-red-900/20 text-gray-400 hover:text-red-500"
										title={$i18n?.t('Delete') ?? 'Delete'}
										on:click={() => handleDelete(group)}
									>
										<svg
											xmlns="http://www.w3.org/2000/svg"
											class="w-4 h-4"
											viewBox="0 0 20 20"
											fill="currentColor"
										>
											<path
												fill-rule="evenodd"
												d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z"
												clip-rule="evenodd"
											/>
										</svg>
									</button>
								</td>
							{/if}
						</tr>
					{/each}
				</tbody>
			</table>
		</div>

		{#if totalCount > pageSize}
			<div class="mt-4 flex justify-between items-center px-4 py-2 border-t border-gray-100 dark:border-gray-800">
				<div class="text-xs text-gray-500 dark:text-gray-400">
					{$i18n?.t('Total') ?? 'Total'}: {totalCount} | {$i18n?.t('Page') ?? 'Page'}: {currentPage} / {Math.ceil(totalCount / pageSize)}
				</div>
				<Pagination
					bind:page={currentPage}
					count={totalCount}
					perPage={pageSize}
				/>
			</div>
		{/if}
	</div>
{/if}