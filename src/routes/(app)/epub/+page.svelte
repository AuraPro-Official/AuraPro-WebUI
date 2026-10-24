<script lang="ts">
	import { getContext, onDestroy, onMount } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as I18n } from 'i18next';
	import { user, WEBUI_NAME, showSidebar, mobile, showArchivedChats } from '$lib/stores';
	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import UserMenu from '$lib/components/layout/Sidebar/UserMenu.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Sidebar from '$lib/components/icons/Sidebar.svelte';
	import BookOpen from '$lib/components/icons/BookOpen.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import ArrowPath from '$lib/components/icons/ArrowPath.svelte';
	import Cog6 from '$lib/components/icons/Cog6.svelte';
	import ChevronLeft from '$lib/components/icons/ChevronLeft.svelte';
	import ChevronRight from '$lib/components/icons/ChevronRight.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import {
		getEpubBook,
		getEpubBooks,
		getEpubPassages,
		searchEpub,
		type EpubBook,
		type EpubBookDetail,
		type EpubPassagePage,
		type EpubSearchHit,
		type EpubSearchResponse
	} from '$lib/apis/epub';

	const i18n: Writable<I18n> = getContext('i18n');
	const pageSize = 50;
	const resultViews = ['fused', 'graph', 'vector'] as const;
	type ResultView = (typeof resultViews)[number];
	let loadingBooks = true;
	let loadingBook = false;
	let loadingPassages = false;
	let searching = false;
	let books: EpubBook[] = [];
	let selectedBookId = '';
	let selectedBook: EpubBookDetail | null = null;
	let selectedVersionId = '';
	let passages: EpubPassagePage | null = null;
	let passageOffset = 0;
	let query = '';
	let bookFilter = '';
	let searchResult: EpubSearchResponse | null = null;
	let resultView: ResultView = 'fused';
	let booksError = '';
	let bookError = '';
	let passageError = '';
	let searchError = '';
	let bookRequest = 0;
	let passageRequest = 0;
	let searchRequest = 0;
	let listRequest = 0;
	let destroyed = false;
	let reader: HTMLElement;

	$: filteredBooks = books.filter((book) =>
		book.title.toLocaleLowerCase().includes(bookFilter.trim().toLocaleLowerCase())
	);
	const hitsFor = (result: EpubSearchResponse, view: ResultView) =>
		view === 'graph'
			? result.graph_results
			: view === 'vector'
				? result.vector_results
				: result.fused_results;
	$: visibleHits = searchResult ? hitsFor(searchResult, resultView) : [];
	$: selectedVersion = selectedBook?.versions.find(
		(version) => version.version_id === selectedVersionId
	);
	const token = () => localStorage.token ?? '';
	const errorMessage = (error: unknown) => (error instanceof Error ? error.message : String(error));
	const tocLabel = (path: string[] | undefined) =>
		path?.filter(Boolean).join(' / ') || $i18n.t('Uncategorized chapter');
	const statusLabel = (status: string | null) =>
		$i18n.t(
			(
				{ READY: 'Ready', PARSING: 'Processing', FAILED: 'Failed', ARCHIVED: 'Archived' } as Record<
					string,
					string
				>
			)[status ?? ''] || 'Not ready'
		);
	const resultLabel = (view: ResultView) =>
		$i18n.t(
			{ fused: 'Combined results', graph: 'Graph matches', vector: 'Semantic matches' }[view]
		);
	const clearSearch = () => {
		searchRequest += 1;
		searching = false;
		query = '';
		searchResult = null;
		searchError = '';
	};
	const loadBooks = async () => {
		const request = ++listRequest;
		loadingBooks = true;
		booksError = '';
		try {
			const next = await getEpubBooks(token());
			if (destroyed || request !== listRequest) return;
			books = next;
			if (books.length) {
				await selectBook(
					books.some((book) => book.book_id === selectedBookId) ? selectedBookId : books[0].book_id,
					false
				);
			} else {
				bookRequest += 1;
				passageRequest += 1;
				selectedBookId = '';
				selectedBook = null;
				selectedVersionId = '';
				passages = null;
				loadingBook = false;
				loadingPassages = false;
			}
		} catch (error) {
			if (!destroyed && request === listRequest) booksError = errorMessage(error);
		} finally {
			if (!destroyed && request === listRequest) loadingBooks = false;
		}
	};
	const selectBook = async (bookId: string, leaveSearch = true) => {
		if (leaveSearch) clearSearch();
		const request = ++bookRequest;
		passageRequest += 1;
		selectedBookId = bookId;
		selectedBook = null;
		selectedVersionId = '';
		passages = null;
		loadingPassages = false;
		loadingBook = true;
		bookError = '';
		passageError = '';
		try {
			const next = await getEpubBook(token(), bookId);
			if (destroyed || request !== bookRequest) return;
			selectedBook = next;
			selectedVersionId =
				next.current_version_id ??
				next.versions.find((version) => version.status === 'READY')?.version_id ??
				next.versions[0]?.version_id ??
				'';
			passageOffset = 0;
			await loadPassages();
		} catch (error) {
			if (!destroyed && request === bookRequest) bookError = errorMessage(error);
		} finally {
			if (!destroyed && request === bookRequest) loadingBook = false;
		}
	};
	const loadPassages = async () => {
		const request = ++passageRequest;
		passages = null;
		passageError = '';
		loadingPassages = false;
		if (
			!selectedVersionId ||
			!selectedBook?.versions.some(
				(version) =>
					version.version_id === selectedVersionId && ['READY', 'ARCHIVED'].includes(version.status)
			)
		)
			return;
		loadingPassages = true;
		try {
			const next = await getEpubPassages(token(), selectedVersionId, passageOffset, pageSize);
			if (destroyed || request !== passageRequest) return;
			passages = next;
			reader?.scrollTo({ top: 0 });
		} catch (error) {
			if (!destroyed && request === passageRequest) passageError = errorMessage(error);
		} finally {
			if (!destroyed && request === passageRequest) loadingPassages = false;
		}
	};
	const changeVersion = () => {
		passageOffset = 0;
		void loadPassages();
	};
	const changePassagePage = (nextOffset: number) => {
		passageOffset = Math.max(0, nextOffset);
		void loadPassages();
	};
	const search = async (graphOffset = 0, searchQuery = query.trim(), resetView = true) => {
		if (!searchQuery) return;
		const request = ++searchRequest;
		searching = true;
		searchError = '';
		try {
			const next = await searchEpub(token(), {
				query: searchQuery,
				graph_offset: graphOffset,
				graph_limit: 20,
				vector_limit: 10
			});
			if (destroyed || request !== searchRequest) return;
			searchResult = next;
			if (resetView) resultView = 'fused';
			reader?.scrollTo({ top: 0 });
		} catch (error) {
			if (!destroyed && request === searchRequest) searchError = errorMessage(error);
		} finally {
			if (!destroyed && request === searchRequest) searching = false;
		}
	};
	onMount(() => {
		void loadBooks();
	});
	onDestroy(() => {
		destroyed = true;
	});
</script>

<svelte:head><title>{$i18n.t('EPUB Library')} • {$WEBUI_NAME}</title></svelte:head>

<div
	class="flex h-screen max-h-[100dvh] w-full max-w-full flex-col transition-width duration-200 ease-in-out {$showSidebar
		? 'md:max-w-[calc(100%-var(--sidebar-width))]'
		: ''}"
>
	<nav
		class="w-full shrink-0 px-2 pt-1.5 backdrop-blur-xl drag-region"
		aria-label={$i18n.t('EPUB Library')}
	>
		<div class="flex items-center">
			{#if $mobile}
				<Tooltip content={$showSidebar ? $i18n.t('Close Sidebar') : $i18n.t('Open Sidebar')}>
					<button
						type="button"
						id="sidebar-toggle-button"
						class="rounded-lg p-1.5 transition hover:bg-gray-100 dark:hover:bg-gray-850"
						aria-label={$showSidebar ? $i18n.t('Close Sidebar') : $i18n.t('Open Sidebar')}
						on:click={() => showSidebar.set(!$showSidebar)}><Sidebar /></button
					>
				</Tooltip>
			{/if}
			<div class="ml-2 flex min-w-0 flex-1 items-center justify-between gap-2 py-0.5">
				<div class="flex min-w-0 items-center gap-2 text-sm font-medium">
					<BookOpen className="size-4 shrink-0" /><span class="truncate"
						>{$i18n.t('EPUB Library')}</span
					><span class="text-xs font-normal text-gray-500">{books.length}</span>
				</div>
				<div class="flex shrink-0 items-center gap-1">
					<Tooltip content={$i18n.t('Refresh')}
						><button
							type="button"
							class="rounded-lg p-2 text-gray-500 transition hover:bg-gray-100 disabled:opacity-40 dark:hover:bg-gray-850"
							disabled={loadingBooks}
							aria-label={$i18n.t('Refresh')}
							on:click={loadBooks}><ArrowPath className="size-4" /></button
						></Tooltip
					>
					{#if $user?.role === 'admin'}<Tooltip content={$i18n.t('Manage library')}
							><a
								href="/admin/epub"
								class="rounded-lg p-2 text-gray-500 transition hover:bg-gray-100 dark:hover:bg-gray-850"
								aria-label={$i18n.t('Manage library')}><Cog6 className="size-4" /></a
							></Tooltip
						>{/if}
					{#if $user}
						<UserMenu
							className="w-[240px]"
							role={$user.role}
							help={true}
							on:show={(event) => {
								if (event.detail === 'archived-chat') showArchivedChats.set(true);
							}}
						>
							<button
								type="button"
								class="select-none rounded-lg p-1.5 transition hover:bg-gray-50 dark:hover:bg-gray-850"
								aria-label={$i18n.t('User Menu')}
								><img
									src={WEBUI_API_BASE_URL + '/users/' + $user.id + '/profile/image'}
									class="size-6 rounded-full object-cover"
									alt={$i18n.t('User profile')}
									draggable="false"
								/></button
							>
						</UserMenu>
					{/if}
				</div>
			</div>
		</div>
	</nav>

	<main class="flex min-h-0 flex-1 flex-col px-3 pb-4 md:px-6">
		<div class="mx-auto flex min-h-0 w-full max-w-7xl flex-1 flex-col pt-4">
			<form
				class="mb-4 flex shrink-0 items-center gap-2"
				role="search"
				aria-label={$i18n.t('Search library')}
				on:submit|preventDefault={() => search()}
			>
				<div class="relative min-w-0 flex-1">
					<div
						class="pointer-events-none absolute inset-y-0 left-3 flex items-center text-gray-400"
					>
						<Search className="size-4" />
					</div>
					<input
						type="search"
						bind:value={query}
						aria-label={$i18n.t('Search concepts or terms')}
						placeholder={$i18n.t('Search concepts or terms')}
						class="h-10 w-full rounded-lg border-0 bg-gray-50 py-2 pl-9 pr-10 text-sm outline-none focus:ring-1 focus:ring-gray-300 dark:bg-gray-850 dark:focus:ring-gray-700"
					/>
					{#if query || searchResult}<Tooltip
							className="absolute inset-y-0 right-1 flex items-center"
							content={$i18n.t('Clear search')}
							><button
								type="button"
								class="rounded-md p-1.5 text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-800"
								aria-label={$i18n.t('Clear search')}
								on:click={clearSearch}><XMark className="size-4" /></button
							></Tooltip
						>{/if}
				</div>
				<Tooltip content={$i18n.t('Search')}
					><button
						type="submit"
						class="flex size-10 shrink-0 items-center justify-center rounded-lg bg-gray-900 text-white transition disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900"
						disabled={searching || !query.trim()}
						aria-label={$i18n.t('Search')}
						>{#if searching}<Spinner />{:else}<Search className="size-4" />{/if}</button
					></Tooltip
				>
			</form>
			{#if booksError}<div
					role="alert"
					class="mb-3 flex items-center justify-between gap-3 text-sm text-red-600 dark:text-red-400"
				>
					<span class="break-words">{booksError}</span><button
						type="button"
						class="shrink-0 underline"
						on:click={loadBooks}>{$i18n.t('Retry')}</button
					>
				</div>{/if}
			{#if searchError}<div
					role="alert"
					class="mb-3 flex items-center justify-between gap-3 text-sm text-red-600 dark:text-red-400"
				>
					<span class="break-words">{searchError}</span><button
						type="button"
						class="shrink-0 underline"
						on:click={() => search()}>{$i18n.t('Retry')}</button
					>
				</div>{/if}

			<div class="flex min-h-0 flex-1 flex-col gap-4 md:flex-row md:gap-6">
				<aside
					class="hidden w-56 shrink-0 flex-col border-r border-gray-100 pr-4 dark:border-gray-800 md:flex"
					aria-label={$i18n.t('Shared bookshelf')}
				>
					<div class="mb-3 flex items-center justify-between gap-2 text-sm font-medium">
						<h2>{$i18n.t('Shared bookshelf')}</h2>
						<span class="text-xs font-normal text-gray-500">{books.length}</span>
					</div>
					<div class="relative mb-2">
						<div
							class="pointer-events-none absolute inset-y-0 left-2 flex items-center text-gray-400"
						>
							<Search className="size-3.5" />
						</div>
						<input
							type="search"
							bind:value={bookFilter}
							aria-label={$i18n.t('Filter books')}
							placeholder={$i18n.t('Filter books')}
							class="h-8 w-full rounded-md border-0 bg-transparent pl-7 pr-2 text-xs outline-none focus:bg-gray-50 dark:focus:bg-gray-850"
						/>
					</div>
					<div class="min-h-0 flex-1 overflow-y-auto">
						{#if loadingBooks && !books.length}<div
								class="flex justify-center py-8"
								role="status"
								aria-label={$i18n.t('Loading...')}
							>
								<Spinner />
							</div>
						{:else if !filteredBooks.length}<p class="py-4 text-xs text-gray-500">
								{$i18n.t(books.length ? 'No results found' : 'No books yet')}
							</p>
						{:else}{#each filteredBooks as book (book.book_id)}
								<button
									type="button"
									class="mb-1 flex w-full items-start gap-2 rounded-lg p-2.5 text-left transition hover:bg-gray-50 dark:hover:bg-gray-850 {selectedBookId ===
									book.book_id
										? 'bg-gray-100 dark:bg-gray-850'
										: ''}"
									aria-pressed={selectedBookId === book.book_id}
									on:click={() => selectBook(book.book_id)}
								>
									<BookOpen className="mt-0.5 size-4 shrink-0 text-gray-400" /><span
										class="min-w-0 flex-1"
										><span class="line-clamp-2 break-words text-sm font-medium">{book.title}</span
										><span class="mt-1 flex items-center gap-1.5 text-xs text-gray-500"
											><span
												class="size-1.5 shrink-0 rounded-full {book.current_version_status ===
												'READY'
													? 'bg-emerald-500'
													: book.current_version_status === 'FAILED'
														? 'bg-red-500'
														: 'bg-gray-300 dark:bg-gray-600'}"
											></span>{statusLabel(book.current_version_status)}</span
										></span
									>
								</button>
							{/each}{/if}
					</div>
				</aside>

				<div class="shrink-0 md:hidden">
					<label class="sr-only" for="epub-book">{$i18n.t('Shared bookshelf')}</label><select
						id="epub-book"
						class="h-10 w-full rounded-lg border-0 bg-gray-50 px-3 text-sm dark:bg-gray-850"
						value={selectedBookId}
						disabled={!books.length}
						on:change={(event) => selectBook(event.currentTarget.value)}
						>{#if !books.length}<option value="">{$i18n.t('No books yet')}</option
							>{/if}{#each books as book (book.book_id)}<option value={book.book_id}
								>{book.title}</option
							>{/each}</select
					>
				</div>

				<div
					class="flex min-h-0 min-w-0 flex-1 flex-col"
					aria-busy={searching || loadingBook || loadingPassages}
				>
					{#if searchResult}
						<div class="flex shrink-0 items-start justify-between gap-3 pb-3">
							<div class="min-w-0">
								<h1 class="break-words text-lg font-semibold">{searchResult.query}</h1>
								{#if searchResult.resolved_concepts.length}<p
										class="mt-1 break-words text-xs text-gray-500"
									>
										{searchResult.resolved_concepts.join(' · ')}
									</p>{/if}
							</div>
							<Tooltip content={$i18n.t('Back to reading')}
								><button
									type="button"
									class="shrink-0 rounded-lg p-2 text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850"
									aria-label={$i18n.t('Back to reading')}
									on:click={clearSearch}><BookOpen className="size-4" /></button
								></Tooltip
							>
						</div>
						<div
							class="mb-3 flex shrink-0 gap-4 overflow-x-auto border-b border-gray-100 dark:border-gray-800"
							role="group"
							aria-label={$i18n.t('Search results')}
						>
							{#each resultViews as view}<button
									type="button"
									class="flex shrink-0 items-center gap-1.5 border-b-2 py-2 text-sm transition {resultView ===
									view
										? 'border-gray-900 font-medium text-gray-900 dark:border-gray-100 dark:text-gray-100'
										: 'border-transparent text-gray-500 hover:text-gray-900 dark:hover:text-gray-100'}"
									aria-pressed={resultView === view}
									on:click={() => {
										resultView = view;
										reader?.scrollTo({ top: 0 });
									}}
									>{resultLabel(view)}<span class="text-xs font-normal text-gray-400"
										>{view === 'graph'
											? searchResult.graph_total
											: hitsFor(searchResult, view).length}</span
									></button
								>{/each}
						</div>
						{#if searchResult.degraded.length}<div
								role="status"
								class="mb-3 shrink-0 break-words text-xs text-amber-700 dark:text-amber-400"
							>
								{$i18n.t('Limited search')}: {searchResult.degraded
									.map((item) => item.component + (item.reason ? ': ' + item.reason : ''))
									.join(' · ')}
							</div>{/if}
						<div
							bind:this={reader}
							class="min-h-0 flex-1 overflow-y-auto pr-1"
							aria-label={$i18n.t('Search results')}
						>
							{#if !visibleHits.length}<div
									class="flex min-h-48 flex-col items-center justify-center gap-3 text-gray-500"
								>
									<Search className="size-7 text-gray-300 dark:text-gray-600" />
									<p class="text-sm">{$i18n.t('No results found')}</p>
								</div>{:else}{#each visibleHits as hit (hit.passage_id + ':' + hit.excerpt.start_codepoint)}{@render SearchHit(
										hit
									)}{/each}{/if}
						</div>
						{#if resultView === 'graph'}{@render Pagination(
								searchResult.graph_offset,
								searchResult.graph_results.length,
								searchResult.graph_total,
								searching,
								(offset) => search(offset, searchResult!.query, false),
								20
							)}{/if}
					{:else if loadingBook && !selectedBook}
						<div
							class="flex flex-1 items-center justify-center"
							role="status"
							aria-label={$i18n.t('Loading...')}
						>
							<Spinner className="size-5" />
						</div>
					{:else if bookError}
						<div
							class="flex flex-1 flex-col items-center justify-center gap-3 text-center"
							role="alert"
						>
							<p class="break-words text-sm text-red-600 dark:text-red-400">{bookError}</p>
							<button
								type="button"
								class="text-sm underline"
								on:click={() => selectBook(selectedBookId)}>{$i18n.t('Retry')}</button
							>
						</div>
					{:else if selectedBook}
						<header
							class="mb-3 flex shrink-0 flex-col items-start justify-between gap-3 border-b border-gray-100 pb-4 dark:border-gray-800 sm:flex-row"
						>
							<div class="min-w-0 flex-1">
								<h1 class="break-words text-lg font-semibold">{selectedBook.title}</h1>
								<p class="mt-1 text-xs text-gray-500">{$i18n.t('Original text')}</p>
							</div>
							<label class="flex max-w-full items-center gap-2 text-xs text-gray-500"
								>{$i18n.t('Version')}<select
									aria-label={$i18n.t('Version')}
									bind:value={selectedVersionId}
									on:change={changeVersion}
									class="h-8 min-w-0 max-w-48 rounded-md border-0 bg-gray-50 px-2 text-xs text-gray-700 dark:bg-gray-850 dark:text-gray-200"
									>{#each selectedBook.versions as version (version.version_id)}<option
											value={version.version_id}
											>{version.version_id.slice(0, 8)} · {statusLabel(version.status)}</option
										>{/each}</select
								></label
							>
						</header>
						<div
							bind:this={reader}
							class="min-h-0 flex-1 overflow-y-auto pr-1"
							aria-label={$i18n.t('Original text')}
						>
							{#if loadingPassages}<div
									class="flex justify-center py-16"
									role="status"
									aria-label={$i18n.t('Loading...')}
								>
									<Spinner />
								</div>
							{:else if passageError}<div role="alert" class="space-y-3 py-8">
									<p class="break-words text-sm text-red-600 dark:text-red-400">{passageError}</p>
									<button type="button" class="text-sm underline" on:click={loadPassages}
										>{$i18n.t('Retry')}</button
									>
								</div>
							{:else if !passages?.items.length}<p class="py-12 text-center text-sm text-gray-500">
									{$i18n.t(
										selectedVersion?.status === 'FAILED'
											? 'Book processing failed'
											: selectedVersion?.status === 'PARSING'
												? 'Book is being processed'
												: 'No passages available'
									)}
								</p>
							{:else}{#each passages.items as passage (passage.passage_id)}<article
										class="border-b border-gray-100 py-5 first:pt-1 last:border-0 dark:border-gray-800"
									>
										<p class="mb-2 break-words text-xs text-gray-500">
											{tocLabel(passage.toc_path)}
										</p>
										<p class="whitespace-pre-wrap break-words text-sm leading-7">
											{passage.content}
										</p>
									</article>{/each}{/if}
						</div>
						{#if passages}{@render Pagination(
								passageOffset,
								passages.items.length,
								passages.total,
								loadingPassages,
								changePassagePage,
								pageSize
							)}{/if}
					{:else}
						<div
							class="flex flex-1 flex-col items-center justify-center gap-3 py-12 text-center text-gray-500"
						>
							<BookOpen className="size-8 text-gray-300 dark:text-gray-600" />
							<p class="text-sm">{$i18n.t(loadingBooks ? 'Loading...' : 'No books yet')}</p>
							{#if !loadingBooks && $user?.role === 'admin'}<a
									href="/admin/epub"
									class="text-sm text-gray-700 underline dark:text-gray-300"
									>{$i18n.t('Manage library')}</a
								>{/if}
						</div>
					{/if}
				</div>
			</div>
		</div>
	</main>
</div>

{#snippet Pagination(
	offset: number,
	count: number,
	total: number,
	busy: boolean,
	change: (offset: number) => void | Promise<void>,
	size: number
)}
	<div
		class="mt-3 flex h-11 shrink-0 items-center justify-between gap-3 border-t border-gray-100 pt-2 text-xs text-gray-500 dark:border-gray-800"
	>
		<Tooltip content={$i18n.t('Previous page')}
			><button
				type="button"
				class="rounded-lg p-2 hover:bg-gray-100 disabled:opacity-30 dark:hover:bg-gray-850"
				disabled={busy || offset === 0}
				aria-label={$i18n.t('Previous page')}
				on:click={() => change(Math.max(0, offset - size))}
				><ChevronLeft className="size-4" /></button
			></Tooltip
		>
		<span class="tabular-nums"
			>{total === 0 ? 0 : offset + 1}–{Math.min(offset + count, total)} / {total}</span
		>
		<Tooltip content={$i18n.t('Next page')}
			><button
				type="button"
				class="rounded-lg p-2 hover:bg-gray-100 disabled:opacity-30 dark:hover:bg-gray-850"
				disabled={busy || offset + count >= total}
				aria-label={$i18n.t('Next page')}
				on:click={() => change(offset + size)}><ChevronRight className="size-4" /></button
			></Tooltip
		>
	</div>
{/snippet}

{#snippet SearchHit(hit: EpubSearchHit)}
	<article class="border-b border-gray-100 py-5 first:pt-1 last:border-0 dark:border-gray-800">
		<h2 class="break-words text-sm font-medium">{hit.book_title}</h2>
		<p class="mt-1 break-words text-xs text-gray-500">
			{tocLabel(hit.toc_path)} · {hit.provenance.join(' / ')}
		</p>
		{#if hit.matched_concepts.length}<p class="mt-2 break-words text-xs text-gray-500">
				{hit.matched_concepts.join(' · ')}
			</p>{/if}
		<blockquote class="my-3 border-l-2 border-gray-200 pl-3 dark:border-gray-700">
			<p class="mb-1 text-xs text-gray-400">
				{$i18n.t('Exact excerpt')} · {hit.excerpt.start_codepoint}–{hit.excerpt.end_codepoint}
			</p>
			<p class="whitespace-pre-wrap break-words text-sm text-gray-600 dark:text-gray-300">
				{hit.excerpt.content}
			</p>
		</blockquote>
		<p class="whitespace-pre-wrap break-words text-sm leading-7">{hit.content}</p>
	</article>
{/snippet}
