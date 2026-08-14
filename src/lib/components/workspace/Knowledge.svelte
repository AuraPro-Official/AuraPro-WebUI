<script lang="ts">
	import dayjs from 'dayjs';
	import relativeTime from 'dayjs/plugin/relativeTime';
	dayjs.extend(relativeTime);

	import { toast } from 'svelte-sonner';
	import { onMount, getContext, tick, onDestroy } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import type { Socket } from 'socket.io-client';

	const i18n = getContext<Writable<i18nType>>('i18n');

	import { WEBUI_NAME, knowledge, user } from '$lib/stores';
	import { socket } from '$lib/stores';
	import {
		deleteKnowledgeById,
		searchKnowledgeBases,
		exportKnowledgeById,
		importKnowledgeWithVectors
	} from '$lib/apis/knowledge';

	import { goto } from '$app/navigation';
	import { capitalizeFirstLetter } from '$lib/utils';

	import DeleteConfirmDialog from '../common/ConfirmDialog.svelte';
	import ItemMenu from './Knowledge/ItemMenu.svelte';
	import Badge from '../common/Badge.svelte';
	import Search from '../icons/Search.svelte';
	import Plus from '../icons/Plus.svelte';
	import Spinner from '../common/Spinner.svelte';
	import Tooltip from '../common/Tooltip.svelte';
	import XMark from '../icons/XMark.svelte';
	import ViewSelector from './common/ViewSelector.svelte';
	import Loader from '../common/Loader.svelte';

	type KnowledgeListItem = {
		id: string;
		name: string;
		description?: string;
		updated_at: number;
		write_access?: boolean;
		meta?: any;
		user?: {
			name?: string;
			email?: string;
		};
	};

	type KnowledgeExportProgressEvent = {
		data?: {
			type?: string;
			data?: {
				request_id?: string;
				percent?: number;
				message?: string;
			};
		};
	};

	let loaded = false;
	let showDeleteConfirm = false;
	let tagsContainerElement: HTMLDivElement;

	let importInput: HTMLInputElement;
	let importTarget: KnowledgeListItem | null = null;
	let selectedItem: KnowledgeListItem | null = null;

	let exportProgress = false;
	let exportProgressPercent = 0;
	let exportProgressMessage = '';
	let exportError: string | null = null;
	let exportSocketEventHandler: ((event: KnowledgeExportProgressEvent) => void) | null = null;
	let exportActiveSocket: Socket | null = null;
	let exportRequestId: string | null = null;
	let exportController: AbortController | null = null;

	let importProgress = false;
	let importProgressPercent = 0;
	let importProgressMessage = '';
	let importUploading = false;
	let importError: string | null = null;

	let page = 1;
	let query = '';
	let searchDebounceTimer: ReturnType<typeof setTimeout>;
	let viewOption = '';
	let sourceOption = '';

	let items: KnowledgeListItem[] | null = null;
	let total: number | null = null;

	let allItemsLoaded = false;
	let itemsLoading = false;

	const handleSearchInput = () => {
		clearTimeout(searchDebounceTimer);
		searchDebounceTimer = setTimeout(() => {
			init();
		}, 300);
	};

	onDestroy(() => {
		clearTimeout(searchDebounceTimer);
		if (exportActiveSocket && exportSocketEventHandler) {
			exportActiveSocket.off('events', exportSocketEventHandler);
		}
	});

	$: if (loaded && viewOption !== undefined && sourceOption !== undefined) {
		init();
	}

	const reset = () => {
		page = 1;
		items = null;
		total = null;
		allItemsLoaded = false;
		itemsLoading = false;
	};

	const loadMoreItems = async () => {
		if (allItemsLoaded) return;
		page += 1;
		await getItemsPage();
	};

	const init = async () => {
		if (!loaded) return;

		reset();
		await getItemsPage();
	};

	const getItemsPage = async () => {
		itemsLoading = true;
		const res = await searchKnowledgeBases(
			localStorage.token,
			query,
			viewOption,
			page,
			sourceOption
		).catch(() => {
			return [];
		});

		if (res) {
			console.log(res);
			total = res.total;
			const pageItems: KnowledgeListItem[] = res.items ?? [];

			if ((pageItems ?? []).length === 0) {
				allItemsLoaded = true;
			} else {
				allItemsLoaded = false;
			}

			if (items) {
				const existingIds = new Set(items.map((item) => item.id));
				const newItems = pageItems.filter((item) => !existingIds.has(item.id));
				items = [...items, ...newItems];
			} else {
				items = pageItems;
			}
		}

		itemsLoading = false;
		return res;
	};

	const deleteHandler = async (item: KnowledgeListItem | null) => {
		if (!item) return;

		const res = await deleteKnowledgeById(localStorage.token, item.id).catch((e) => {
			toast.error(`${e}`);
		});

		if (res) {
			toast.success($i18n.t('Knowledge deleted successfully.'));
			init();
		}
	};

	const exportHandler = async (item: KnowledgeListItem) => {
		const requestId =
			crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
		exportRequestId = requestId;

		const controller = new AbortController();
		exportController = controller;

		exportProgress = true;
		exportProgressPercent = 0;
		exportProgressMessage = $i18n.t('Preparing export...') ?? '正在准备导出...';
		exportError = null;

		try {
			const res = await exportKnowledgeById(
				localStorage.token,
				item.id,
				requestId,
				controller.signal
			);

			if (res.body) {
				const blob = await new Response(res.body).blob();
				const url = URL.createObjectURL(blob);
				const a = document.createElement('a');
				a.href = url;
				a.download = `${item.name}_with_vectors.zip`;
				document.body.appendChild(a);
				a.click();
				a.remove();
				URL.revokeObjectURL(url);
				exportProgressPercent = 100;
				toast.success($i18n.t('Knowledge exported successfully'));
			} else {
				throw new Error('Empty export response');
			}
		} catch (e) {
			if (controller.signal.aborted) {
				return;
			}
			controller.abort();
			exportError = (e as any)?.detail ?? `${e}`;
			toast.error(exportError ?? '');
		} finally {
			if (exportError) {
				return;
			}
			setTimeout(() => {
				exportProgress = false;
				exportRequestId = null;
				exportController = null;
			}, 500);
		}
	};

	const closeExport = () => {
		exportController?.abort();
		exportProgress = false;
		exportError = null;
		exportRequestId = null;
		exportController = null;
	};

	const handleExportSocketEvent = (event: KnowledgeExportProgressEvent) => {
		if (event?.data?.type !== 'knowledge:export_progress') return;

		const payload = event?.data?.data ?? {};
		if (!exportRequestId || payload?.request_id !== exportRequestId) return;

		if (typeof payload?.percent === 'number') {
			exportProgressPercent = payload.percent;
		}
		if (payload?.message) {
			exportProgressMessage = payload.message;
		}
	};

	const importHandler = async (item) => {
		importTarget = item;
		importInput?.click();
	};

	const handleFileChange = async (e: Event) => {
		const el = e.target as HTMLInputElement;
		const file = el?.files?.[0];
		if (!file) return;
		el.value = '';

		importError = null;
		importProgress = true;
		importProgressPercent = 0;
		importProgressMessage = $i18n.t('Uploading archive...') ?? '正在上传压缩包...';
		importUploading = true;

		try {
			const res = await importKnowledgeWithVectors(
				localStorage.token,
				file,
				importTarget.id,
				(progress) => {
					if (typeof progress?.progress === 'number') {
						importProgressPercent = progress.progress;
					}
					if (progress?.message) {
						importProgressMessage = progress.message;
					}
					if (progress?.progress != null) {
						importUploading = false;
					}
				}
			);
			importProgressPercent = 100;
			importUploading = false;
			toast.success($i18n.t('Knowledge imported successfully'));
			init();
			if (res && res.id) {
				goto(`/workspace/knowledge/${res.id}`);
			}
		} catch (err) {
			importError = `${err}`;
			toast.error(importError);
		} finally {
			if (!importError) {
				setTimeout(() => {
					importProgress = false;
				}, 500);
			}
		}
	};

	onMount(async () => {
		viewOption = localStorage?.workspaceViewOption || '';
		sourceOption = localStorage?.workspaceKnowledgeSourceOption || '';
		loaded = true;

		exportSocketEventHandler = handleExportSocketEvent;
		socket.subscribe((value) => {
			if (exportActiveSocket && exportSocketEventHandler) {
				exportActiveSocket.off('events', exportSocketEventHandler);
			}
			exportActiveSocket = value;
			if (exportActiveSocket && exportSocketEventHandler) {
				exportActiveSocket.on('events', exportSocketEventHandler);
			}
		});
	});
</script>

<svelte:head>
	<title>
		{$i18n.t('Knowledge')} • {$WEBUI_NAME}
	</title>
</svelte:head>

{#if loaded}
	<DeleteConfirmDialog
		bind:show={showDeleteConfirm}
		on:confirm={() => {
			deleteHandler(selectedItem);
		}}
	/>

	{#if exportProgress}
		<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4">
			<div
				class="w-full max-w-sm rounded-2xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-6 shadow-xl"
			>
				{#if exportError}
					<div class="flex items-center justify-between mb-3">
						<div class="text-sm font-medium text-red-500">
							{$i18n.t('Export failed')}
						</div>
					</div>
					<div
						class="text-xs text-gray-500 dark:text-gray-400 break-words max-h-24 overflow-y-auto mb-4"
					>
						{exportError}
					</div>
					<button
						class="w-full py-2 rounded-xl bg-gray-100 dark:bg-gray-800 text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-200 dark:hover:bg-gray-700 transition"
						on:click={closeExport}
					>
						{$i18n.t('Close')}
					</button>
				{:else}
					<div class="flex items-center justify-between mb-3">
						<div class="text-sm font-medium text-gray-700 dark:text-gray-200">
							{$i18n.t('Exporting knowledge...') ?? '正在导出知识库...'}
						</div>
						<div class="text-xs font-semibold text-gray-500 dark:text-gray-400">
							{exportProgressPercent}%
						</div>
					</div>
					<div class="w-full h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
						<div
							class="h-1.5 bg-blue-500 rounded-full transition-all duration-300"
							style="width: {exportProgressPercent}%"
						></div>
					</div>
					<div class="mt-2 text-xs text-gray-500 dark:text-gray-400 truncate">
						{exportProgressMessage}
					</div>
				{/if}
			</div>
		</div>
	{/if}

	{#if importProgress}
		<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4">
			<div
				class="w-full max-w-sm rounded-2xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-6 shadow-xl"
			>
				{#if importError}
					<div class="flex items-center justify-between mb-3">
						<div class="text-sm font-medium text-red-500">
							{$i18n.t('Import failed')}
						</div>
					</div>
					<div
						class="text-xs text-gray-500 dark:text-gray-400 break-words max-h-24 overflow-y-auto mb-4"
					>
						{importError}
					</div>
					<button
						class="w-full py-2 rounded-xl bg-gray-100 dark:bg-gray-800 text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-200 dark:hover:bg-gray-700 transition"
						on:click={() => {
							importError = null;
							importProgress = false;
						}}
					>
						{$i18n.t('Close')}
					</button>
				{:else}
					<div class="flex items-center justify-between mb-3">
						<div class="text-sm font-medium text-gray-700 dark:text-gray-200">
							{$i18n.t('Importing knowledge...') ?? '正在导入知识库...'}
						</div>
						<div class="text-xs font-semibold text-gray-500 dark:text-gray-400">
							{importUploading ? '' : `${importProgressPercent}%`}
						</div>
					</div>
					<div class="w-full h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
						{#if importUploading}
							<div class="h-1.5 bg-blue-500 rounded-full w-1/2 animate-pulse"></div>
						{:else}
							<div
								class="h-1.5 bg-blue-500 rounded-full transition-all duration-300"
								style="width: {importProgressPercent}%"
							></div>
						{/if}
					</div>
					<div class="mt-2 text-xs text-gray-500 dark:text-gray-400 truncate">
						{importProgressMessage}
					</div>
				{/if}
			</div>
		</div>
	{/if}

	<input
		type="file"
		accept=".zip"
		bind:this={importInput}
		on:change={handleFileChange}
		style="display:none"
	/>

	<div class="flex flex-col gap-1 px-1 mt-1.5 mb-3">
		<div class="flex justify-between items-center">
			<div class="flex items-center md:self-center text-xl font-medium px-0.5 gap-2 shrink-0">
				<div>
					{$i18n.t('Knowledge')}
				</div>

				<div class="text-lg font-medium text-gray-500 dark:text-gray-500">
					{total}
				</div>
			</div>

			<div class="flex w-full justify-end gap-1.5">
				<a
					class=" px-2 py-1.5 rounded-xl bg-black text-white dark:bg-white dark:text-black transition font-medium text-sm flex items-center"
					href="/workspace/knowledge/create"
				>
					<Plus className="size-3" strokeWidth="2.5" />

					<div class=" hidden md:block md:ml-1 text-xs">{$i18n.t('New Knowledge')}</div>
				</a>
			</div>
		</div>
	</div>

	<div
		class="py-2 bg-white dark:bg-gray-900 rounded-3xl border border-gray-100/30 dark:border-gray-850/30"
	>
		<div class=" flex w-full space-x-2 py-0.5 px-3.5 pb-2">
			<div class="flex flex-1">
				<div class=" self-center ml-1 mr-3">
					<Search className="size-3.5" />
				</div>
				<input
					class=" w-full text-sm py-1 rounded-r-xl outline-hidden bg-transparent"
					bind:value={query}
					on:input={handleSearchInput}
					aria-label={$i18n.t('Search Knowledge')}
					placeholder={$i18n.t('Search Knowledge')}
				/>
				{#if query}
					<div class="self-center pl-1.5 translate-y-[0.5px] rounded-l-xl bg-transparent">
						<button
							class="p-0.5 rounded-full hover:bg-gray-100 dark:hover:bg-gray-900 transition"
							aria-label={$i18n.t('Clear search')}
							on:click={() => {
								query = '';
								handleSearchInput();
							}}
						>
							<XMark className="size-3" strokeWidth="2" />
						</button>
					</div>
				{/if}
			</div>
		</div>

		<div
			class="px-3 flex w-full bg-transparent overflow-x-auto scrollbar-none -mx-1"
			on:wheel={(e) => {
				if (e.deltaY !== 0) {
					e.preventDefault();
					e.currentTarget.scrollLeft += e.deltaY;
				}
			}}
		>
			<div
				class="flex gap-0.5 w-fit text-center text-sm rounded-full bg-transparent px-1.5 whitespace-nowrap"
				bind:this={tagsContainerElement}
			>
				<ViewSelector
					bind:value={viewOption}
					onChange={async (value) => {
						localStorage.workspaceViewOption = value;

						await tick();
					}}
				/>

				<select
					class="relative w-full flex items-center gap-0.5 px-2.5 py-1.5 bg-gray-50 dark:bg-gray-850 rounded-xl outline-hidden"
					bind:value={sourceOption}
					on:change={async () => {
						localStorage.workspaceKnowledgeSourceOption = sourceOption;
						await tick();
					}}
				>
					<option value="">{$i18n.t('All Sources')}</option>
					<option value="local">{$i18n.t('Local')}</option>
					<option value="external">{$i18n.t('Connected')}</option>
				</select>
			</div>
		</div>

		{#if items !== null && total !== null}
			{#if (items ?? []).length !== 0}
				<div class=" my-2 px-3 grid grid-cols-1 lg:grid-cols-2 gap-2">
					{#each items as item}
						<button
							class=" flex space-x-4 cursor-pointer text-left w-full px-3 py-2.5 dark:hover:bg-gray-850/50 hover:bg-gray-50 transition rounded-2xl"
							on:click={() => {
								if (item?.meta?.document) {
									toast.error(
										$i18n.t(
											'Only collections can be edited, create a new knowledge base to edit/add documents.'
										)
									);
								} else {
									goto(`/workspace/knowledge/${item.id}`);
								}
							}}
						>
							<div class=" w-full">
								<div class=" self-center flex-1 justify-between">
									<div class="flex items-center justify-between -my-1 h-8">
										<div class=" flex gap-2 items-center justify-between w-full">
											{#if item?.meta?.source === 'external'}
												<div>
													<Badge
														type="muted"
														content={item?.meta?.external?.provider ?? $i18n.t('Connected')}
													/>
												</div>
												<div>
													<Badge type="muted" content={$i18n.t('Read Only')} />
												</div>
											{:else}
												<div>
													<Badge
														type="success"
														content={item?.meta?.knowledge_type === 'bilingual'
															? $i18n.t('Bilingual')
															: $i18n.t('Collection')}
													/>
												</div>
											{/if}

											{#if !item?.write_access && item?.meta?.source !== 'external'}
												<div>
													<Badge type="muted" content={$i18n.t('Read Only')} />
												</div>
											{/if}
										</div>

										{#if item?.write_access || $user?.role === 'admin'}
											<div class="flex items-center gap-2">
												<div class=" flex self-center">
													<ItemMenu
														onExport={$user?.role === 'admin' &&
														item?.meta?.knowledge_type === 'bilingual'
															? () => {
																	exportHandler(item);
																}
															: null}
														onImport={$user?.role === 'admin' &&
														item?.meta?.knowledge_type === 'bilingual'
															? () => {
																	importHandler(item);
																}
															: null}
														on:delete={() => {
															selectedItem = item;
															showDeleteConfirm = true;
														}}
													/>
												</div>
											</div>
										{/if}
									</div>

									<div class=" flex items-center gap-1 justify-between px-1.5">
										<Tooltip content={item?.description ?? item.name}>
											<div class=" flex items-center gap-2">
												<div class=" text-sm font-medium line-clamp-1 capitalize">{item.name}</div>
											</div>
										</Tooltip>

										<div class="flex items-center gap-2 shrink-0">
											<Tooltip content={dayjs(item.updated_at * 1000).format('LLLL')}>
												<div class=" text-xs text-gray-500 line-clamp-1 hidden sm:block">
													{$i18n.t('Updated')}
													{dayjs(item.updated_at * 1000).fromNow()}
												</div>
											</Tooltip>

											<div class="text-xs text-gray-500 shrink-0">
												<Tooltip
													content={item?.user?.email ?? $i18n.t('Deleted User')}
													className="flex shrink-0"
													placement="top-start"
												>
													{$i18n.t('By {{name}}', {
														name: capitalizeFirstLetter(
															item?.user?.name ?? item?.user?.email ?? $i18n.t('Deleted User')
														)
													})}
												</Tooltip>
											</div>
										</div>
									</div>
								</div>
							</div>
						</button>
					{/each}
				</div>

				{#if !allItemsLoaded}
					<Loader
						on:visible={(e) => {
							if (!itemsLoading) {
								loadMoreItems();
							}
						}}
					>
						<div class="w-full flex justify-center py-4 text-xs animate-pulse items-center gap-2">
							<Spinner className=" size-4" />
							<div class=" ">{$i18n.t('Loading...')}</div>
						</div>
					</Loader>
				{/if}
			{:else}
				<div class=" w-full h-full flex flex-col justify-center items-center my-16 mb-24">
					<div class="max-w-md text-center">
						<div class=" text-3xl mb-3">😕</div>
						<div class=" text-lg font-medium mb-1">{$i18n.t('No knowledge found')}</div>
						<div class=" text-gray-500 text-center text-xs">
							{$i18n.t('Try adjusting your search or filter to find what you are looking for.')}
						</div>
					</div>
				</div>
			{/if}
		{:else}
			<div class="w-full h-full flex justify-center items-center py-10">
				<Spinner className="size-4" />
			</div>
		{/if}
	</div>

	<div class=" text-gray-500 text-xs m-2">
		ⓘ {$i18n.t("Use '#' in the prompt input to load and include your knowledge.")}
	</div>
{:else}
	<div class="w-full h-full flex justify-center items-center">
		<Spinner className="size-5" />
	</div>
{/if}
