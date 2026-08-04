<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { toast } from 'svelte-sonner';

	import { user, WEBUI_NAME } from '$lib/stores';
	import {
		createEpubBatchDraft,
		getEpubBook,
		getEpubBooks,
		importEpub,
		indexEpubVersion,
		indexEpubRetrievalUnit,
		pollEpubBatch,
		runEpubLocalCalibration,
		retryEpubBatch,
		submitEpubBatch,
		upsertEpubConcept,
		type BatchStatus,
		type EpubBook,
		type EpubBookDetail,
		type EpubVersionIndexResult,
		type LocalCalibrationReport
	} from '$lib/apis/epub';

	const token = () => localStorage.token ?? '';
	const errorMessage = (error: unknown) => (error instanceof Error ? error.message : String(error));

	let loading = true;
	let busy = false;
	let books: EpubBook[] = [];
	let selectedBook: EpubBookDetail | null = null;
	let selectedVersionId = '';
	let selectedFile: File | null = null;
	let profileName = '';
	let promptProfile = 'zh-glossary-v3';
	let sampleOnly = true;
	let sampleLimit = 20;
	let batchJobId = '';
	let batchState: BatchStatus | null = null;
	let calibrationState: LocalCalibrationReport | null = null;
	let conceptName = '';
	let conceptAliases = '';
	let conceptDefinition = '';
	let conceptStatus: 'PROVISIONAL' | 'APPROVED' | 'REJECTED' = 'APPROVED';
	let retrievalUnitId = '';
	let versionIndexState: EpubVersionIndexResult | null = null;

	const chooseFile = (event: Event) => {
		selectedFile = (event.currentTarget as HTMLInputElement).files?.[0] ?? null;
	};

	const loadBooks = async () => {
		loading = true;
		try {
			books = await getEpubBooks(token());
			if (books.length > 0) await selectBook(books[0].book_id);
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			loading = false;
		}
	};

	const selectBook = async (bookId: string) => {
		try {
			selectedBook = await getEpubBook(token(), bookId);
			selectedVersionId = selectedBook.current_version_id ?? selectedBook.versions[0]?.version_id ?? '';
		} catch (error) {
			toast.error(errorMessage(error));
		}
	};

	const upload = async () => {
		if (!selectedFile) return;
		busy = true;
		try {
			const result = await importEpub(token(), selectedFile);
			toast.success(result.duplicate ? '该 EPUB 已存在，复用了已有版本。' : 'EPUB 已导入，等待后续离线构建。');
			selectedFile = null;
			await loadBooks();
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const createBatch = async () => {
		if (!selectedVersionId || !profileName.trim()) return;
		busy = true;
		try {
			const result = await createEpubBatchDraft(token(), {
				version_id: selectedVersionId,
				profile_name: profileName.trim(),
				prompt_profile: promptProfile,
				is_sample: sampleOnly,
				sample_limit: sampleLimit
			});
			batchJobId = result.batch_job_id;
			batchState = result;
			toast.success(`已创建 ${result.item_count} 项离线 Batch 草稿。`);
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const runBatchAction = async (action: 'submit' | 'poll' | 'retry') => {
		if (!batchJobId) return;
		busy = true;
		try {
			batchState = await ({ submit: submitEpubBatch, poll: pollEpubBatch, retry: retryEpubBatch }[action])(token(), batchJobId);
			if (action === 'retry' && typeof batchState.batch_job_id === 'string') batchJobId = batchState.batch_job_id;
			toast.success(action === 'submit' ? 'Batch 已提交到服务器管理员配置的离线 Provider。' : action === 'poll' ? '已更新 Batch 状态。' : '已创建失败项重试 Batch。');
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const runLocalCalibration = async () => {
		if (!selectedVersionId) return;
		busy = true;
		try {
			calibrationState = await runEpubLocalCalibration(token(), {
				version_id: selectedVersionId,
				prompt_profile: promptProfile,
				sample_limit: Math.min(100, Math.max(1, sampleLimit))
			});
			const { valid_items: valid, sample_count: total } = calibrationState;
			if (valid === total) toast.success(`本地校准完成：${valid}/${total} 项通过 schema 与原文 offset 校验。`);
			else toast.error(`本地校准完成：${valid}/${total} 项有效。请先优化 prompt，再创建云端样本。`);
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const saveConcept = async () => {
		if (!conceptName.trim()) return;
		busy = true;
		try {
			await upsertEpubConcept(token(), {
				canonical_name: conceptName.trim(),
				aliases: conceptAliases.split(/[\n,]/).map((value) => value.trim()).filter(Boolean),
				definition: conceptDefinition.trim(),
				status: conceptStatus
			});
			toast.success('概念已保存。');
			conceptName = '';
			conceptAliases = '';
			conceptDefinition = '';
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const indexUnit = async () => {
		if (!retrievalUnitId.trim()) return;
		busy = true;
		try {
			const result = await indexEpubRetrievalUnit(token(), retrievalUnitId.trim());
			toast.success(`索引请求完成：${String(result.state ?? 'unknown')}`);
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const indexVersion = async (rebuild: boolean) => {
		if (!selectedVersionId) return;
		busy = true;
		try {
			versionIndexState = await indexEpubVersion(token(), selectedVersionId, rebuild);
			const { ready, degraded, failed, selected_retrieval_units: selected } = versionIndexState;
			if (degraded || failed) {
				toast.error(`索引完成 ${selected} 项：就绪 ${ready}，降级 ${degraded}，失败 ${failed}。请查看下方详情。`);
			} else {
				toast.success(selected ? `已建立 ${ready} 个派生向量索引。` : '所有派生向量均已就绪。');
			}
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	onMount(async () => {
		if ($user?.role !== 'admin') {
			await goto('/epub');
			return;
		}
		await loadBooks();
	});
</script>

<svelte:head><title>EPUB 管理 • {$WEBUI_NAME}</title></svelte:head>

<main class="mx-auto w-full max-w-5xl space-y-6 px-4 py-6 sm:px-6">
	<header class="flex flex-wrap items-start justify-between gap-3"><div><h1 class="text-xl font-semibold">EPUB 概念图书馆管理</h1><p class="mt-1 text-sm text-gray-500 dark:text-gray-400">导入、离线 Batch、概念和索引操作均由服务器管理员执行；客户端不会传递任何 Provider 密钥。</p></div><a class="rounded border px-3 py-2 text-sm" href="/epub">返回图书馆</a></header>

	<section class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"><h2 class="font-medium">导入文本 EPUB</h2><p class="mt-1 text-xs text-gray-500">仅支持文本内容；图片和表格会在解析报告中标记为超出首期范围。</p><div class="mt-3 flex flex-wrap items-center gap-3"><input aria-label="选择 EPUB 文件" type="file" accept=".epub,application/epub+zip" on:change={chooseFile} /><button class="rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50" disabled={busy || !selectedFile} on:click={upload}>导入 EPUB</button></div></section>

	<section class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"><div class="flex items-center justify-between"><h2 class="font-medium">共享书库版本</h2><button class="text-sm text-blue-600" on:click={loadBooks}>刷新</button></div>{#if loading}<p class="mt-3 text-sm text-gray-500">正在加载…</p>{:else if books.length === 0}<p class="mt-3 text-sm text-gray-500">尚未导入 EPUB。</p>{:else}<div class="mt-3 grid gap-2 sm:grid-cols-2">{#each books as book (book.book_id)}<button class="rounded border p-3 text-left text-sm hover:bg-gray-50 dark:hover:bg-gray-800 {selectedBook?.book_id === book.book_id ? 'border-blue-500' : 'border-gray-200 dark:border-gray-800'}" on:click={() => selectBook(book.book_id)}><span class="block font-medium">{book.title}</span><span class="text-xs text-gray-500">{book.current_version_status ?? '未就绪'}</span></button>{/each}</div>{/if}{#if selectedBook}<label class="mt-4 block text-sm">当前版本 <select bind:value={selectedVersionId} class="ml-2 rounded border bg-transparent px-2 py-1">{#each selectedBook.versions as version (version.version_id)}<option value={version.version_id}>{version.version_id.slice(0, 8)} · {version.status}</option>{/each}</select></label>{/if}</section>

	<section class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"><h2 class="font-medium">本地 Prompt 校准</h2><p class="mt-1 text-xs text-gray-500">先在 Desktop 托管的 Qwen 上做跨章节结构与 offset 校验。本地通过不代表云端模型质量，且不会发送任何段落到云端。</p><div class="mt-3 grid gap-3 sm:grid-cols-2"><label class="text-sm">Prompt profile<select bind:value={promptProfile} class="mt-1 w-full rounded border bg-transparent px-2 py-1"><option value="zh-glossary-v3">zh-glossary-v3</option><option value="zh-glossary-v2">zh-glossary-v2（历史）</option><option value="zh-glossary-v1">zh-glossary-v1（历史）</option></select></label><label class="text-sm">样本上限<input bind:value={sampleLimit} min="1" max="100" type="number" class="mt-1 w-full rounded border bg-transparent px-2 py-1" /></label></div><button class="mt-3 rounded border px-3 py-2 text-sm disabled:opacity-50" disabled={busy || !selectedVersionId} on:click={runLocalCalibration}>运行本地校准</button>{#if calibrationState}<div class="mt-4 rounded bg-gray-50 p-3 text-sm dark:bg-gray-800"><p>模型：{calibrationState.model}；样本 {calibrationState.sample_count} 项，覆盖 {calibrationState.chapter_count} 个章节；有效 {calibrationState.valid_items}，无效 {calibrationState.invalid_items}；概念 {calibrationState.concept_count}，mentions {calibrationState.mention_count}。</p>{#if calibrationState.invalid_items}<ul class="mt-2 list-disc space-y-1 pl-5 text-xs text-red-700 dark:text-red-300">{#each calibrationState.items.filter((item) => !item.valid) as item (item.passage_id)}<li>段落 #{item.ordinal}：{item.reason ?? '无效输出'}</li>{/each}</ul>{/if}</div>{/if}</section>

	<section class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"><h2 class="font-medium">离线概念 Batch</h2><p class="mt-1 text-xs text-gray-500">仅在本地校准与管理员审阅后创建。样本会跨章节均匀抽取；轮询和失败项重试不会从浏览器接收凭证。</p><div class="mt-3 grid gap-3 sm:grid-cols-3"><label class="text-sm">固定模型快照<input bind:value={profileName} class="mt-1 w-full rounded border bg-transparent px-2 py-1" placeholder="例如 gpt-4o-mini-YYYY-MM-DD" /></label><label class="text-sm">Prompt profile<select bind:value={promptProfile} class="mt-1 w-full rounded border bg-transparent px-2 py-1"><option value="zh-glossary-v3">zh-glossary-v3</option><option value="zh-glossary-v2">zh-glossary-v2（历史）</option><option value="zh-glossary-v1">zh-glossary-v1（历史）</option></select></label><label class="text-sm">样本上限<input bind:value={sampleLimit} min="1" max="500" type="number" class="mt-1 w-full rounded border bg-transparent px-2 py-1" /></label></div><label class="mt-3 flex items-center gap-2 text-sm"><input bind:checked={sampleOnly} type="checkbox" />仅创建样本任务</label><button class="mt-3 rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50" disabled={busy || !selectedVersionId || !profileName.trim()} on:click={createBatch}>创建 Batch 草稿</button>{#if batchJobId}<div class="mt-4 rounded bg-gray-50 p-3 text-sm dark:bg-gray-800"><p>任务：<code>{batchJobId}</code></p>{#if batchState}<pre class="mt-2 overflow-auto text-xs">{JSON.stringify(batchState, null, 2)}</pre>{/if}<div class="mt-3 flex flex-wrap gap-2"><button class="rounded border px-2 py-1 text-xs disabled:opacity-50" disabled={busy} on:click={() => runBatchAction('submit')}>提交</button><button class="rounded border px-2 py-1 text-xs disabled:opacity-50" disabled={busy} on:click={() => runBatchAction('poll')}>轮询状态</button><button class="rounded border px-2 py-1 text-xs disabled:opacity-50" disabled={busy} on:click={() => runBatchAction('retry')}>重试失败项</button></div></div>{/if}</section>

	<section class="grid gap-6 lg:grid-cols-2"><form class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900" on:submit|preventDefault={saveConcept}><h2 class="font-medium">概念审核与维护</h2><label class="mt-3 block text-sm">标准名称<input bind:value={conceptName} required class="mt-1 w-full rounded border bg-transparent px-2 py-1" /></label><label class="mt-3 block text-sm">别名（逗号或换行分隔）<textarea bind:value={conceptAliases} class="mt-1 w-full rounded border bg-transparent px-2 py-1"></textarea></label><label class="mt-3 block text-sm">定义<textarea bind:value={conceptDefinition} class="mt-1 w-full rounded border bg-transparent px-2 py-1"></textarea></label><label class="mt-3 block text-sm">状态<select bind:value={conceptStatus} class="mt-1 w-full rounded border bg-transparent px-2 py-1"><option value="APPROVED">已批准</option><option value="PROVISIONAL">待审核</option><option value="REJECTED">已拒绝</option></select></label><button class="mt-3 rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50" disabled={busy || !conceptName.trim()}>保存概念</button></form><section class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"><h2 class="font-medium">派生向量索引</h2><p class="mt-1 text-xs text-gray-500">按当前书籍版本批量建立向量索引；只处理派生 retrieval unit，原文 passage 永远不会被替代。</p><div class="mt-3 flex flex-wrap gap-2"><button class="rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50" disabled={busy || !selectedVersionId} on:click={() => indexVersion(false)}>索引未就绪项</button><button class="rounded border px-3 py-2 text-sm disabled:opacity-50" disabled={busy || !selectedVersionId} on:click={() => indexVersion(true)}>重建当前版本</button></div>{#if versionIndexState}<div class="mt-3 rounded bg-gray-50 p-3 text-sm dark:bg-gray-800"><p>模式：{versionIndexState.mode === 'REBUILD' ? '重建全部' : '仅未就绪项'}；共 {versionIndexState.total_retrieval_units} 项，本次 {versionIndexState.selected_retrieval_units} 项，就绪 {versionIndexState.ready}，降级 {versionIndexState.degraded}，失败 {versionIndexState.failed}。</p>{#if versionIndexState.errors.length}<ul class="mt-2 list-disc space-y-1 pl-5 text-xs text-red-700 dark:text-red-300">{#each versionIndexState.errors as error (error.retrieval_unit_id)}<li><code>{error.retrieval_unit_id}</code>：{error.reason}</li>{/each}</ul>{/if}</div>{/if}<details class="mt-4 border-t pt-3"><summary class="cursor-pointer text-xs text-gray-500">按 retrieval unit ID 单项诊断</summary><form class="mt-2" on:submit|preventDefault={indexUnit}><label class="block text-sm">Retrieval unit ID<input bind:value={retrievalUnitId} required class="mt-1 w-full rounded border bg-transparent px-2 py-1" /></label><button class="mt-3 rounded border px-3 py-2 text-sm disabled:opacity-50" disabled={busy || !retrievalUnitId.trim()}>建立单项索引</button></form></details></section></section>
</main>
