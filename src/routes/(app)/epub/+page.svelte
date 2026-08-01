<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { user, WEBUI_NAME } from '$lib/stores';
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

	const pageSize = 50;

	let loadingBooks = true;
	let loadingPassages = false;
	let searching = false;
	let books: EpubBook[] = [];
	let selectedBook: EpubBookDetail | null = null;
	let selectedVersionId = '';
	let passages: EpubPassagePage | null = null;
	let passageOffset = 0;
	let query = '';
	let searchResult: EpubSearchResponse | null = null;

	const token = () => localStorage.token ?? '';
	const errorMessage = (error: unknown) => (error instanceof Error ? error.message : String(error));

	const loadBooks = async () => {
		loadingBooks = true;
		try {
			books = await getEpubBooks(token());
			if (!selectedBook && books.length > 0) {
				await selectBook(books[0].book_id);
			}
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			loadingBooks = false;
		}
	};

	const selectBook = async (bookId: string) => {
		try {
			selectedBook = await getEpubBook(token(), bookId);
			selectedVersionId =
				selectedBook.current_version_id ?? selectedBook.versions.find((version) => version.status === 'READY')?.version_id ?? '';
			passageOffset = 0;
			await loadPassages();
		} catch (error) {
			toast.error(errorMessage(error));
		}
	};

	const loadPassages = async () => {
		if (!selectedVersionId) {
			passages = null;
			return;
		}
		loadingPassages = true;
		try {
			passages = await getEpubPassages(token(), selectedVersionId, passageOffset, pageSize);
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			loadingPassages = false;
		}
	};

	const changeVersion = async () => {
		passageOffset = 0;
		await loadPassages();
	};

	const changePassagePage = async (nextOffset: number) => {
		passageOffset = Math.max(0, nextOffset);
		await loadPassages();
	};

	const search = async (graphOffset = 0) => {
		if (!query.trim()) return;
		searching = true;
		try {
			searchResult = await searchEpub(token(), {
				query: query.trim(),
				graph_offset: graphOffset,
				graph_limit: 20,
				vector_limit: 10
			});
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			searching = false;
		}
	};

	const tocLabel = (path: string[] | undefined) => path?.filter(Boolean).join(' / ') || '未编排章节';

	onMount(() => {
		void loadBooks();
	});
</script>

<svelte:head>
	<title>EPUB 概念图书馆 • {$WEBUI_NAME}</title>
</svelte:head>

<main class="mx-auto flex w-full max-w-7xl flex-col gap-6 px-4 py-6 sm:px-6">
	<header class="flex flex-wrap items-start justify-between gap-3">
		<div>
			<h1 class="text-xl font-semibold text-gray-900 dark:text-gray-100">EPUB 概念图书馆</h1>
			<p class="mt-1 max-w-3xl text-sm text-gray-500 dark:text-gray-400">
				检索结果始终显示完整原文段落；高亮摘录仅是该段落的精确连续片段。
			</p>
		</div>
		{#if $user?.role === 'admin'}
			<a class="rounded-lg bg-gray-900 px-3 py-2 text-sm text-white hover:bg-gray-700 dark:bg-gray-100 dark:text-gray-900" href="/admin/epub">
				管理图书与离线任务
			</a>
		{/if}
	</header>

	<section class="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-800 dark:bg-gray-900">
		<form class="flex flex-col gap-3 sm:flex-row" on:submit|preventDefault={() => search(0)}>
			<label class="sr-only" for="epub-search">检索概念或术语</label>
			<input
				id="epub-search"
				bind:value={query}
				class="min-w-0 flex-1 rounded-lg border border-gray-300 bg-transparent px-3 py-2 text-sm outline-none focus:border-gray-600 dark:border-gray-700"
				placeholder="检索概念或术语"
			/>
			<button class="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-50" disabled={searching}>
				{searching ? '检索中…' : '检索'}
			</button>
		</form>
		<p class="mt-2 text-xs text-gray-500 dark:text-gray-400">仅使用服务器本地或私有网络模型；不可用时会明确显示降级状态。</p>
	</section>

	{#if searchResult}
		<section class="space-y-4" aria-live="polite">
			<div class="flex flex-wrap items-baseline justify-between gap-2">
				<div>
					<h2 class="text-lg font-medium">概念检索：{searchResult.query}</h2>
					<p class="text-sm text-gray-500 dark:text-gray-400">
						{searchResult.graph_total} 个图谱命中
						{#if searchResult.resolved_concepts.length > 0}
							· 已解析：{searchResult.resolved_concepts.join('、')}
						{/if}
					</p>
				</div>
				{#if searchResult.degraded.length > 0}
					<p class="rounded bg-amber-50 px-2 py-1 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-200">
						降级：{searchResult.degraded.map((item) => `${item.component}${item.reason ? `（${item.reason}）` : ''}`).join('；')}
					</p>
				{/if}
			</div>

			<div class="grid gap-4 xl:grid-cols-2">
				<article class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900">
					<h3 class="font-medium">图谱全部匹配</h3>
					<p class="mb-3 text-xs text-gray-500 dark:text-gray-400">此通道可分页穷尽所有概念出现位置。</p>
					{#if searchResult.graph_results.length === 0}
						<p class="text-sm text-gray-500">没有当前页结果。</p>
					{:else}
						<div class="space-y-3">
							{#each searchResult.graph_results as hit (hit.passage_id + hit.excerpt.start_codepoint)}
								{@render SearchHit(hit)}
							{/each}
						</div>
					{/if}
					<div class="mt-4 flex justify-between">
						<button class="rounded border px-2 py-1 text-xs disabled:opacity-50" disabled={searching || searchResult.graph_offset === 0} on:click={() => search(Math.max(0, searchResult.graph_offset - 20))}>上一页</button>
						<button class="rounded border px-2 py-1 text-xs disabled:opacity-50" disabled={searching || searchResult.graph_offset + searchResult.graph_results.length >= searchResult.graph_total} on:click={() => search(searchResult.graph_offset + 20)}>下一页</button>
					</div>
				</article>

				<article class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900">
					<h3 class="font-medium">语义相关段落</h3>
					<p class="mb-3 text-xs text-gray-500 dark:text-gray-400">派生向量窗口经本地重排和 MMR 去重，引用仍为完整原文段落。</p>
					{#if searchResult.vector_results.length === 0}
						<p class="text-sm text-gray-500">没有语义结果或本地向量服务不可用。</p>
					{:else}
						<div class="space-y-3">
							{#each searchResult.vector_results as hit (hit.passage_id + hit.excerpt.start_codepoint)}
								{@render SearchHit(hit)}
							{/each}
						</div>
					{/if}
				</article>
			</div>
		</section>
	{/if}

	<section class="grid gap-6 lg:grid-cols-[17rem_minmax(0,1fr)]">
		<aside class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900">
			<div class="mb-3 flex items-center justify-between"><h2 class="font-medium">共享书架</h2><button class="text-xs text-blue-600" on:click={loadBooks}>刷新</button></div>
			{#if loadingBooks}
				<p class="text-sm text-gray-500">正在加载…</p>
			{:else if books.length === 0}
				<p class="text-sm text-gray-500">尚未导入文本 EPUB。</p>
			{:else}
				<div class="space-y-1">
					{#each books as book (book.book_id)}
						<button class="w-full rounded-lg px-2 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800 {selectedBook?.book_id === book.book_id ? 'bg-gray-100 dark:bg-gray-800' : ''}" on:click={() => selectBook(book.book_id)}>
							<span class="block truncate">{book.title}</span><span class="text-xs text-gray-500">{book.current_version_status ?? '未就绪'}</span>
						</button>
					{/each}
				</div>
			{/if}
		</aside>

		<article class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900">
			{#if selectedBook}
				<div class="mb-4 flex flex-wrap items-end justify-between gap-3">
					<div><h2 class="text-lg font-medium">{selectedBook.title}</h2><p class="text-xs text-gray-500">选择版本后按原文段落浏览。</p></div>
					<label class="text-sm">版本 <select bind:value={selectedVersionId} on:change={changeVersion} class="ml-1 rounded border bg-transparent px-2 py-1">{#each selectedBook.versions as version (version.version_id)}<option value={version.version_id}>{version.version_id.slice(0, 8)} · {version.status}</option>{/each}</select></label>
				</div>
				{#if loadingPassages}
					<p class="text-sm text-gray-500">正在加载原文…</p>
				{:else if passages}
					<div class="space-y-4">
						{#each passages.items as passage (passage.passage_id)}
							<div class="border-b border-gray-100 pb-4 last:border-0 dark:border-gray-800"><p class="mb-1 text-xs text-gray-500">{tocLabel(passage.toc_path)} · {passage.content_kind ?? 'paragraph'}</p><p class="whitespace-pre-wrap break-words text-sm leading-6">{passage.content}</p></div>
						{/each}
					</div>
					<div class="mt-5 flex items-center justify-between text-sm"><button class="rounded border px-3 py-1 disabled:opacity-50" disabled={passageOffset === 0} on:click={() => changePassagePage(passageOffset - pageSize)}>上一页</button><span class="text-xs text-gray-500">{passages.total === 0 ? 0 : passageOffset + 1}–{Math.min(passageOffset + passages.items.length, passages.total)} / {passages.total}</span><button class="rounded border px-3 py-1 disabled:opacity-50" disabled={passageOffset + passages.items.length >= passages.total} on:click={() => changePassagePage(passageOffset + pageSize)}>下一页</button></div>
				{/if}
			{:else}
				<p class="text-sm text-gray-500">从书架选择图书以阅读原文。</p>
			{/if}
		</article>
	</section>
</main>

{#snippet SearchHit(hit: EpubSearchHit)}
	<article class="rounded-lg border border-gray-100 p-3 dark:border-gray-800">
		<p class="text-xs text-gray-500">{hit.book_title} · {tocLabel(hit.toc_path)} · {hit.provenance.join(' / ')}</p>
		{#if hit.matched_concepts.length > 0}<p class="mt-1 text-xs text-blue-600">{hit.matched_concepts.join('、')}</p>{/if}
		<p class="mt-2 rounded bg-amber-50 px-2 py-1 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-100">精确摘录 [{hit.excerpt.start_codepoint}, {hit.excerpt.end_codepoint})：{hit.excerpt.content}</p>
		<p class="mt-2 whitespace-pre-wrap break-words text-sm leading-6">{hit.content}</p>
	</article>
{/snippet}
