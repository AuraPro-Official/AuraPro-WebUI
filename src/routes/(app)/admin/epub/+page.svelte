<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { toast } from 'svelte-sonner';

	import { user, WEBUI_NAME } from '$lib/stores';
	import {
		applyEpubOverlay,
		createEpubBatchDraft,
		createEpubSectionGraphBatchDraft,
		getEpubBatchJob,
		getEpubBatchJobs,
		getEpubBook,
		getEpubBooks,
		getEpubPromptProfiles,
		getEpubRelationAssertions,
		getEpubSampleBatchReviews,
		getEpubVersionOverlay,
		importEpub,
		indexEpubVersion,
		indexEpubRetrievalUnit,
		pollEpubBatch,
		recoverEpubBatches,
		runEpubLocalCalibration,
		retryEpubBatch,
		reviewEpubRelationAssertion,
		reviewEpubSampleBatch,
		submitEpubBatch,
		upsertEpubConcept,
		type BatchStatus,
		type EpubBatchRecovery,
		type EpubBatchSummary,
		type EpubBook,
		type EpubBookDetail,
		type EpubOverlayApplyResult,
		type EpubVersionIndexResult,
		type LocalCalibrationReport,
		type RelationAssertion,
		type SampleBatchReview
	} from '$lib/apis/epub';

	const token = () => localStorage.token ?? '';
	const errorMessage = (error: unknown) => (error instanceof Error ? error.message : String(error));

	// 服务器是 prompt profile 的唯一权威。仅当接口不可用时才退回这个值，
	// 以免管理页在无法读取列表时完全无法创建 Batch 或本地校准。
	const FALLBACK_PROMPT_PROFILE = 'zh-glossary-v4';

	let loading = true;
	let busy = false;
	let books: EpubBook[] = [];
	let selectedBook: EpubBookDetail | null = null;
	let selectedVersionId = '';
	let selectedFile: File | null = null;
	let profileName = '';
	let promptProfiles: string[] = [FALLBACK_PROMPT_PROFILE];
	let defaultPromptProfile = FALLBACK_PROMPT_PROFILE;
	let promptProfile = FALLBACK_PROMPT_PROFILE;
	let sampleOnly = true;
	let sampleLimit = 20;
	let batchJobId = '';
	let batchState: BatchStatus | null = null;
	let batchHistory: EpubBatchSummary[] = [];
	let batchDetail: EpubBatchSummary | null = null;
	let batchRecovery: EpubBatchRecovery | null = null;
	let calibrationState: LocalCalibrationReport | null = null;
	let conceptName = '';
	let conceptAliases = '';
	let conceptDefinition = '';
	let conceptStatus: 'PROVISIONAL' | 'APPROVED' | 'REJECTED' = 'APPROVED';
	let retrievalUnitId = '';
	let versionIndexState: EpubVersionIndexResult | null = null;
	let overlayFile: File | null = null;
	let exportedOverlaySha = '';
	let exportedOverlaySummary = '';
	let overlayApplyState: EpubOverlayApplyResult | null = null;
	let relationAssertions: RelationAssertion[] = [];
	let sampleBatchReviews: SampleBatchReview[] = [];

	// 非默认 profile 一律标记为“历史”，这样新增 v5 时无需再改前端。
	$: promptProfileOptions = promptProfiles.map((profileId) => ({
		id: profileId,
		label: profileId === defaultPromptProfile ? profileId : `${profileId}（历史）`
	}));

	const loadPromptProfiles = async () => {
		try {
			const result = await getEpubPromptProfiles(token());
			if (result.prompt_profiles?.length) promptProfiles = result.prompt_profiles;
			defaultPromptProfile = promptProfiles.includes(result.default_prompt_profile)
				? result.default_prompt_profile
				: promptProfiles[0];
			promptProfile = defaultPromptProfile;
		} catch (error) {
			toast.error(errorMessage(error));
		}
	};

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
			selectedVersionId =
				selectedBook.current_version_id ?? selectedBook.versions[0]?.version_id ?? '';
		} catch (error) {
			toast.error(errorMessage(error));
		}
	};

	const upload = async () => {
		if (!selectedFile) return;
		busy = true;
		try {
			const result = await importEpub(token(), selectedFile);
			toast.success(
				result.duplicate ? '该 EPUB 已存在，复用了已有版本。' : 'EPUB 已导入，等待后续离线构建。'
			);
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
			batchDetail = await getEpubBatchJob(token(), batchJobId);
			await loadBatchHistory();
			toast.success(`已创建 ${result.item_count} 项离线 Batch 草稿。`);
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const loadSampleBatchReviews = async () => {
		if (!selectedVersionId) return;
		busy = true;
		try {
			sampleBatchReviews = (
				await getEpubSampleBatchReviews(token(), { version_id: selectedVersionId })
			).items;
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const reviewSampleBatch = async (status: 'APPROVED' | 'REJECTED') => {
		if (!batchJobId) return;
		busy = true;
		try {
			const review = await reviewEpubSampleBatch(token(), batchJobId, status);
			sampleBatchReviews = [
				review,
				...sampleBatchReviews.filter(
					(item) => item.sample_batch_job_id !== review.sample_batch_job_id
				)
			];
			toast.success(
				status === 'APPROVED' ? '云端样本已批准，可以创建同类型全量任务。' : '云端样本已拒绝。'
			);
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const createSectionGraphBatch = async () => {
		if (!selectedVersionId || !profileName.trim()) return;
		busy = true;
		try {
			const result = await createEpubSectionGraphBatchDraft(token(), {
				version_id: selectedVersionId,
				profile_name: profileName.trim(),
				is_sample: sampleOnly,
				sample_limit: sampleLimit
			});
			batchJobId = result.batch_job_id;
			batchState = result;
			batchDetail = await getEpubBatchJob(token(), batchJobId);
			await loadBatchHistory();
			toast.success(`已创建 ${result.item_count} 项章节概念图 Batch 草稿。`);
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const loadBatchHistory = async () => {
		busy = true;
		try {
			batchHistory = (
				await getEpubBatchJobs(token(), { version_id: selectedVersionId || undefined, limit: 50 })
			).items;
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const viewBatch = async (batchJobIdToView: string) => {
		busy = true;
		try {
			batchJobId = batchJobIdToView;
			batchDetail = await getEpubBatchJob(token(), batchJobIdToView);
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const recoverBatches = async () => {
		busy = true;
		try {
			batchRecovery = await recoverEpubBatches(token());
			await loadBatchHistory();
			toast.success(`已恢复轮询 ${batchRecovery.recovered.length} 个未终态任务。`);
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const loadRelationAssertions = async () => {
		if (!selectedVersionId) return;
		busy = true;
		try {
			relationAssertions = (
				await getEpubRelationAssertions(token(), {
					status: 'PROVISIONAL',
					version_id: selectedVersionId
				})
			).items;
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const reviewRelationAssertion = async (assertionId: string, status: 'APPROVED' | 'REJECTED') => {
		busy = true;
		try {
			await reviewEpubRelationAssertion(token(), assertionId, status);
			relationAssertions = relationAssertions.filter((item) => item.assertion_id !== assertionId);
			toast.success(status === 'APPROVED' ? '关系已批准。' : '关系已拒绝。');
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
			batchState = await { submit: submitEpubBatch, poll: pollEpubBatch, retry: retryEpubBatch }[
				action
			](token(), batchJobId);
			if (action === 'retry' && typeof batchState.batch_job_id === 'string')
				batchJobId = batchState.batch_job_id;
			await loadBatchHistory();
			if (action !== 'retry') batchDetail = await getEpubBatchJob(token(), batchJobId);
			toast.success(
				action === 'submit'
					? 'Batch 已提交到服务器管理员配置的离线 Provider。'
					: action === 'poll'
						? '已更新 Batch 状态。'
						: '已创建失败项重试 Batch。'
			);
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
			if (valid === total)
				toast.success(`本地校准完成：${valid}/${total} 项通过 schema 与原文 offset 校验。`);
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
				aliases: conceptAliases
					.split(/[\n,]/)
					.map((value) => value.trim())
					.filter(Boolean),
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

	const chooseOverlayFile = (event: Event) => {
		overlayFile = (event.currentTarget as HTMLInputElement).files?.[0] ?? null;
	};

	// 下载的必须是服务器返回的原始字节：X-Overlay-SHA256 覆盖的正是这些字节，
	// 重新序列化会得到不同的摘要，发布出去就无法校验了。
	const exportOverlay = async () => {
		if (!selectedVersionId) return;
		busy = true;
		try {
			const download = await getEpubVersionOverlay(token(), selectedVersionId);
			const url = URL.createObjectURL(new Blob([download.text], { type: 'application/json' }));
			const anchor = document.createElement('a');
			anchor.href = url;
			anchor.download = `${selectedVersionId.slice(0, 8)}-overlay.json`;
			anchor.click();
			URL.revokeObjectURL(url);
			exportedOverlaySha = download.overlay_sha256;
			exportedOverlaySummary = `${download.overlay.concepts.length} 个概念 · ${download.overlay.mentions.length} 处提及 · ${download.overlay.relations.length} 条关系 · 指纹 ${download.overlay.passage_fingerprint.count} 段`;
			toast.success('分析层已导出；请连同 SHA-256 一起发布。');
		} catch (error) {
			toast.error(errorMessage(error));
		} finally {
			busy = false;
		}
	};

	const applyOverlay = async () => {
		if (!overlayFile) return;
		busy = true;
		try {
			overlayApplyState = await applyEpubOverlay(token(), overlayFile);
			overlayFile = null;
			toast.success(
				`分析层已应用：新增 ${overlayApplyState.applied} 项，跳过 ${overlayApplyState.skipped} 项。请接着重建当前版本的向量索引。`
			);
		} catch (error) {
			overlayApplyState = null;
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
				toast.error(
					`索引完成 ${selected} 项：就绪 ${ready}，降级 ${degraded}，失败 ${failed}。请查看下方详情。`
				);
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
		await loadPromptProfiles();
		await loadBooks();
		await loadBatchHistory();
	});
</script>

<svelte:head><title>EPUB 管理 • {$WEBUI_NAME}</title></svelte:head>

<main class="mx-auto w-full max-w-5xl space-y-6 px-4 py-6 sm:px-6">
	<header class="flex flex-wrap items-start justify-between gap-3">
		<div>
			<h1 class="text-xl font-semibold">EPUB 概念图书馆管理</h1>
			<p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
				导入、离线 Batch、概念和索引操作均由服务器管理员执行；客户端不会传递任何 Provider 密钥。
			</p>
		</div>
		<a class="rounded border px-3 py-2 text-sm" href="/epub">返回图书馆</a>
	</header>

	<section
		class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"
	>
		<h2 class="font-medium">导入文本 EPUB</h2>
		<p class="mt-1 text-xs text-gray-500">
			仅支持文本内容；图片和表格会在解析报告中标记为超出首期范围。
		</p>
		<div class="mt-3 flex flex-wrap items-center gap-3">
			<input
				aria-label="选择 EPUB 文件"
				type="file"
				accept=".epub,application/epub+zip"
				on:change={chooseFile}
			/><button
				class="rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50"
				disabled={busy || !selectedFile}
				on:click={upload}>导入 EPUB</button
			>
		</div>
	</section>

	<section
		class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"
	>
		<div class="flex flex-wrap items-start justify-between gap-3">
			<div>
				<h2 class="font-medium">Batch 历史与恢复</h2>
				<p class="mt-1 text-xs text-gray-500">
					仅显示生命周期与项目计数，不显示云端
					prompt、模型输出或原始错误内容。恢复只轮询已提交/运行中的持久化任务，绝不会提交草稿。
				</p>
			</div>
			<div class="flex gap-2">
				<button
					class="rounded border px-3 py-2 text-sm disabled:opacity-50"
					disabled={busy}
					on:click={loadBatchHistory}>刷新历史</button
				>
				<button
					class="rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50"
					disabled={busy}
					on:click={recoverBatches}>恢复未终态任务</button
				>
			</div>
		</div>
		{#if batchRecovery}<p class="mt-3 text-xs text-gray-500">
				本次恢复：轮询 {batchRecovery.recovered.length} 项；因未配置 Provider 而跳过 {batchRecovery
					.skipped.length} 项。
			</p>{/if}
		{#if batchHistory.length}<ul class="mt-3 space-y-2">
				{#each batchHistory as job (job.batch_job_id)}<li class="rounded border p-3 text-sm">
						<div class="flex flex-wrap items-center justify-between gap-2">
							<div>
								<p class="font-medium"><code>{job.batch_job_id}</code></p>
								<p class="mt-1 text-xs text-gray-500">
									{job.job_kind} · {job.status} · {job.is_sample ? '样本' : '全量'} · {job.item_count}
									项
									{#if job.has_error}· 有错误{/if}
									{#if job.results_pending_retrieval}· 结果待重新获取{/if}
								</p>
							</div>
							<button
								class="rounded border px-2 py-1 text-xs disabled:opacity-50"
								disabled={busy}
								on:click={() => viewBatch(job.batch_job_id)}>查看项目</button
							>
						</div>
					</li>{/each}
			</ul>{:else}<p class="mt-3 text-sm text-gray-500">当前范围没有 Batch 历史。</p>{/if}
		{#if batchDetail}<div class="mt-4 rounded bg-gray-50 p-3 text-sm dark:bg-gray-800">
				<p>
					任务 <code>{batchDetail.batch_job_id}</code>：{batchDetail.status}，项目 {batchDetail.item_count}。
				</p>
				{#if batchDetail.results_pending_retrieval}<p
						class="mt-2 text-xs text-amber-700 dark:text-amber-300"
					>
						云端任务已终态，但结果尚未完整读取。请先再次轮询；在确认每个项目结果前，不会自动创建重试任务。
					</p>{/if}
				{#if batchDetail.items?.length}<ul class="mt-2 space-y-1 text-xs">
						{#each batchDetail.items as item (item.batch_item_id)}<li>
								<code>{item.custom_id}</code> · {item.status} · 尝试 {item.attempt_count}
								{#if item.has_error}· 有错误{/if}
							</li>{/each}
					</ul>{/if}
			</div>{/if}
	</section>

	<section
		class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"
	>
		<div class="flex items-center justify-between">
			<h2 class="font-medium">共享书库版本</h2>
			<button class="text-sm text-blue-600" on:click={loadBooks}>刷新</button>
		</div>
		{#if loading}<p class="mt-3 text-sm text-gray-500">正在加载…</p>{:else if books.length === 0}<p
				class="mt-3 text-sm text-gray-500"
			>
				尚未导入 EPUB。
			</p>{:else}<div class="mt-3 grid gap-2 sm:grid-cols-2">
				{#each books as book (book.book_id)}<button
						class="rounded border p-3 text-left text-sm hover:bg-gray-50 dark:hover:bg-gray-800 {selectedBook?.book_id ===
						book.book_id
							? 'border-blue-500'
							: 'border-gray-200 dark:border-gray-800'}"
						on:click={() => selectBook(book.book_id)}
						><span class="block font-medium">{book.title}</span><span class="text-xs text-gray-500"
							>{book.current_version_status ?? '未就绪'}</span
						></button
					>{/each}
			</div>{/if}{#if selectedBook}<label class="mt-4 block text-sm"
				>当前版本 <select
					bind:value={selectedVersionId}
					class="ml-2 rounded border bg-transparent px-2 py-1"
					>{#each selectedBook.versions as version (version.version_id)}<option
							value={version.version_id}>{version.version_id.slice(0, 8)} · {version.status}</option
						>{/each}</select
				></label
			>{/if}
	</section>

	<section
		class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"
	>
		<h2 class="font-medium">本地 Prompt 校准</h2>
		<p class="mt-1 text-xs text-gray-500">
			先在 Desktop 托管的 Qwen 上做跨章节结构与 offset
			校验。本地通过不代表云端模型质量，且不会发送任何段落到云端。
		</p>
		<div class="mt-3 grid gap-3 sm:grid-cols-2">
			<label class="text-sm"
				>Prompt profile<select
					bind:value={promptProfile}
					class="mt-1 w-full rounded border bg-transparent px-2 py-1"
					>{#each promptProfileOptions as option (option.id)}<option value={option.id}
							>{option.label}</option
						>{/each}</select
				></label
			><label class="text-sm"
				>样本上限<input
					bind:value={sampleLimit}
					min="1"
					max="100"
					type="number"
					class="mt-1 w-full rounded border bg-transparent px-2 py-1"
				/></label
			>
		</div>
		<button
			class="mt-3 rounded border px-3 py-2 text-sm disabled:opacity-50"
			disabled={busy || !selectedVersionId}
			on:click={runLocalCalibration}>运行本地校准</button
		>{#if calibrationState}<div class="mt-4 rounded bg-gray-50 p-3 text-sm dark:bg-gray-800">
				<p>
					模型：{calibrationState.model}；样本 {calibrationState.sample_count} 项，覆盖 {calibrationState.chapter_count}
					个章节；有效 {calibrationState.valid_items}，无效 {calibrationState.invalid_items}；概念 {calibrationState.concept_count}，mentions
					{calibrationState.mention_count}。
				</p>
				{#if calibrationState.invalid_items}<ul
						class="mt-2 list-disc space-y-1 pl-5 text-xs text-red-700 dark:text-red-300"
					>
						{#each calibrationState.items.filter((item) => !item.valid) as item (item.passage_id)}<li
							>
								段落 #{item.ordinal}：{item.reason ?? '无效输出'}
							</li>{/each}
					</ul>{/if}
			</div>{/if}
	</section>

	<section
		class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"
	>
		<h2 class="font-medium">离线概念图 Batch</h2>
		<p class="mt-1 text-xs text-gray-500">
			先经本地校准、完成云端样本并由管理员批准。服务端会拒绝跳过此步骤的同版本、同任务类型全量
			OpenAI Batch；章节图任务会在受限目录范围中同时提取概念、精确原文映射和有证据关系。
		</p>
		<div class="mt-3 grid gap-3 sm:grid-cols-3">
			<label class="text-sm"
				>固定模型快照<input
					bind:value={profileName}
					class="mt-1 w-full rounded border bg-transparent px-2 py-1"
					placeholder="例如 gpt-4o-mini-YYYY-MM-DD"
				/></label
			><label class="text-sm"
				>Prompt profile<select
					bind:value={promptProfile}
					class="mt-1 w-full rounded border bg-transparent px-2 py-1"
					>{#each promptProfileOptions as option (option.id)}<option value={option.id}
							>{option.label}</option
						>{/each}</select
				></label
			><label class="text-sm"
				>样本上限<input
					bind:value={sampleLimit}
					min="1"
					max="500"
					type="number"
					class="mt-1 w-full rounded border bg-transparent px-2 py-1"
				/></label
			>
		</div>
		<label class="mt-3 flex items-center gap-2 text-sm"
			><input bind:checked={sampleOnly} type="checkbox" />仅创建样本任务</label
		>
		<div class="mt-3 flex flex-wrap gap-2">
			<button
				class="rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50"
				disabled={busy || !selectedVersionId || !profileName.trim()}
				on:click={createBatch}>创建概念 Batch 草稿</button
			><button
				class="rounded border px-3 py-2 text-sm disabled:opacity-50"
				disabled={busy || !selectedVersionId || !profileName.trim()}
				on:click={createSectionGraphBatch}>创建章节概念图草稿</button
			>
		</div>
		{#if batchJobId}<div class="mt-4 rounded bg-gray-50 p-3 text-sm dark:bg-gray-800">
				<p>任务：<code>{batchJobId}</code></p>
				{#if batchState}<pre class="mt-2 overflow-auto text-xs">{JSON.stringify(
							batchState,
							null,
							2
						)}</pre>{/if}
				<div class="mt-3 flex flex-wrap gap-2">
					<button
						class="rounded border px-2 py-1 text-xs disabled:opacity-50"
						disabled={busy}
						on:click={() => runBatchAction('submit')}>提交</button
					><button
						class="rounded border px-2 py-1 text-xs disabled:opacity-50"
						disabled={busy}
						on:click={() => runBatchAction('poll')}>轮询状态</button
					><button
						class="rounded border px-2 py-1 text-xs disabled:opacity-50"
						disabled={busy || batchDetail?.results_pending_retrieval}
						on:click={() => runBatchAction('retry')}>重试失败项</button
					>
					{#if batchDetail?.is_sample}<button
							class="rounded bg-blue-600 px-2 py-1 text-xs text-white disabled:opacity-50"
							disabled={busy || batchDetail.status !== 'SUCCEEDED'}
							on:click={() => reviewSampleBatch('APPROVED')}>批准已完成样本</button
						><button
							class="rounded border px-2 py-1 text-xs disabled:opacity-50"
							disabled={busy || batchDetail.status !== 'SUCCEEDED'}
							on:click={() => reviewSampleBatch('REJECTED')}>拒绝已完成样本</button
						>{/if}
				</div>
				<p class="mt-2 text-xs text-gray-500">
					重试只会复制已确认失败且未成功导入的项目；若显示“结果待重新获取”，请先轮询确认，避免重复调用。
				</p>
				{#if batchDetail?.is_sample && batchDetail.status !== 'SUCCEEDED'}<p
						class="mt-2 text-xs text-gray-500"
					>
						只有云端样本处于 SUCCEEDED 且所有项目已成功导入后，才可进行批准或拒绝审核。
					</p>{/if}
			</div>{/if}
		<div class="mt-4 flex items-center justify-between gap-2">
			<p class="text-xs text-gray-500">
				已审核样本仅保存任务标识、审核状态和时间，不复制原文或云端输出。
			</p>
			<button
				class="rounded border px-2 py-1 text-xs disabled:opacity-50"
				disabled={busy || !selectedVersionId}
				on:click={loadSampleBatchReviews}>刷新样本审核</button
			>
		</div>
		{#if sampleBatchReviews.length}<ul
				class="mt-2 space-y-1 text-xs text-gray-600 dark:text-gray-300"
			>
				{#each sampleBatchReviews as review (review.sample_batch_job_id)}<li>
						<code>{review.sample_batch_job_id}</code> · {review.job_kind} · {review.status} · {review.reviewed_at}
					</li>{/each}
			</ul>{/if}
	</section>

	<section
		class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"
	>
		<div class="flex items-center justify-between gap-3">
			<div>
				<h2 class="font-medium">关系断言审核</h2>
				<p class="mt-1 text-xs text-gray-500">
					只显示当前版本的待审核关系。每条关系保留准确原文证据；拒绝只影响这一个版本断言，不会删除其他图书的支持。
				</p>
			</div>
			<button
				class="rounded border px-3 py-2 text-sm disabled:opacity-50"
				disabled={busy || !selectedVersionId}
				on:click={loadRelationAssertions}>刷新待审核关系</button
			>
		</div>
		{#if relationAssertions.length}<ul class="mt-3 space-y-3">
				{#each relationAssertions as assertion (assertion.assertion_id)}<li
						class="rounded border p-3 text-sm"
					>
						<p class="font-medium">
							{assertion.subject_name} <span class="text-gray-500">{assertion.predicate}</span>
							{assertion.object_name}
						</p>
						<ul class="mt-2 list-disc pl-5 text-xs text-gray-600 dark:text-gray-300">
							{#each assertion.evidence as evidence (evidence.passage_id + evidence.start_codepoint)}<li
								>
									<code>{evidence.passage_id.slice(0, 8)}</code> · {evidence.evidence}
								</li>{/each}
						</ul>
						<div class="mt-3 flex gap-2">
							<button
								class="rounded bg-blue-600 px-2 py-1 text-xs text-white disabled:opacity-50"
								disabled={busy}
								on:click={() => reviewRelationAssertion(assertion.assertion_id, 'APPROVED')}
								>批准</button
							><button
								class="rounded border px-2 py-1 text-xs disabled:opacity-50"
								disabled={busy}
								on:click={() => reviewRelationAssertion(assertion.assertion_id, 'REJECTED')}
								>拒绝</button
							>
						</div>
					</li>{/each}
			</ul>{:else}<p class="mt-3 text-sm text-gray-500">
				尚未加载待审核关系，或当前版本没有待审核项。
			</p>{/if}
	</section>

	<section
		class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"
	>
		<h2 class="font-medium">分析层导出与导入</h2>
		<p class="mt-1 text-xs text-gray-500">
			概念图只需由一位管理员用云端 Batch 构建一次，其余安装导入这份分析层即可，无需再付费跑
			Batch。分析层只携带概念名称、别名、定义和<strong>位置</strong>（段落序号 + content_sha256 +
			码点区间），<strong>不含任何原文、证据文本、EPUB 文件或向量</strong
			>；导入方用自己那本书按位置重新取出证据原文。因此双方必须持有同一个 EPUB：archive
			哈希、解析器版本和整本书的段落指纹都必须一致，任何一项不符都会整体拒绝，不会写入半份图。
		</p>
		<div class="mt-3 flex flex-wrap items-center gap-3">
			<button
				class="rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50"
				disabled={busy || !selectedVersionId}
				on:click={exportOverlay}>导出当前版本分析层</button
			>
			<input
				aria-label="选择分析层 JSON 文件"
				type="file"
				accept=".json,application/json"
				on:change={chooseOverlayFile}
			/><button
				class="rounded border px-3 py-2 text-sm disabled:opacity-50"
				disabled={busy || !overlayFile}
				on:click={applyOverlay}>应用分析层</button
			>
		</div>
		{#if exportedOverlaySha}<div class="mt-3 rounded bg-gray-50 p-3 text-sm dark:bg-gray-800">
				<p>已导出：{exportedOverlaySummary}</p>
				<p class="mt-1 break-all text-xs text-gray-500">
					导出文件 SHA-256：<code>{exportedOverlaySha}</code>（请与文件一同发布，供接收方校验）
				</p>
			</div>{/if}
		{#if overlayApplyState}<div class="mt-3 rounded bg-gray-50 p-3 text-sm dark:bg-gray-800">
				<p>
					已应用到版本 <code>{overlayApplyState.version_id.slice(0, 8)}</code>：新增 {overlayApplyState.applied}
					项，跳过 {overlayApplyState.skipped} 项，拒绝 {overlayApplyState.rejected} 项。
				</p>
				<p class="mt-1 text-xs text-gray-500">
					新增明细：概念 {overlayApplyState.applied_detail.concepts_created ?? 0}，更新概念 {overlayApplyState
						.applied_detail.concepts_updated ?? 0}，提及 {overlayApplyState.applied_detail
						.mentions_created ?? 0}，关系 {overlayApplyState.applied_detail.relations_created ??
						0}，关系证据
					{overlayApplyState.applied_detail.relation_evidence_created ?? 0}。
				</p>
				{#if Object.keys(overlayApplyState.skipped_reasons).length}<ul
						class="mt-2 list-disc space-y-1 pl-5 text-xs text-gray-500"
					>
						{#each Object.entries(overlayApplyState.skipped_reasons) as [reason, count] (reason)}<li
							>
								<code>{reason}</code>：{count} 项（本地已有的判定优先，已批准的概念不会被降级，管理员录入的提及不会被模型输出覆盖）
							</li>{/each}
					</ul>{/if}
				<p class="mt-1 break-all text-xs text-gray-500">
					上传文件 SHA-256：<code>{overlayApplyState.uploaded_overlay_sha256}</code>
					{#if overlayApplyState.canonical_overlay_sha256 !== overlayApplyState.uploaded_overlay_sha256}
						· 规范化后 SHA-256：<code>{overlayApplyState.canonical_overlay_sha256}</code>
					{/if}
				</p>
				{#if overlayApplyState.vectors_require_reindex}<p
						class="mt-2 text-xs text-amber-700 dark:text-amber-300"
					>
						导入的分析层不含向量。请在下方“派生向量索引”中对当前版本执行<strong>重建当前版本</strong
						>，新概念才可被检索。
					</p>{/if}
			</div>{/if}
	</section>

	<section class="grid gap-6 lg:grid-cols-2">
		<form
			class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"
			on:submit|preventDefault={saveConcept}
		>
			<h2 class="font-medium">概念审核与维护</h2>
			<label class="mt-3 block text-sm"
				>标准名称<input
					bind:value={conceptName}
					required
					class="mt-1 w-full rounded border bg-transparent px-2 py-1"
				/></label
			><label class="mt-3 block text-sm"
				>别名（逗号或换行分隔）<textarea
					bind:value={conceptAliases}
					class="mt-1 w-full rounded border bg-transparent px-2 py-1"
				></textarea></label
			><label class="mt-3 block text-sm"
				>定义<textarea
					bind:value={conceptDefinition}
					class="mt-1 w-full rounded border bg-transparent px-2 py-1"
				></textarea></label
			><label class="mt-3 block text-sm"
				>状态<select
					bind:value={conceptStatus}
					class="mt-1 w-full rounded border bg-transparent px-2 py-1"
					><option value="APPROVED">已批准</option><option value="PROVISIONAL">待审核</option
					><option value="REJECTED">已拒绝</option></select
				></label
			><button
				class="mt-3 rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50"
				disabled={busy || !conceptName.trim()}>保存概念</button
			>
		</form>
		<section
			class="rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900"
		>
			<h2 class="font-medium">派生向量索引</h2>
			<p class="mt-1 text-xs text-gray-500">
				按当前书籍版本批量建立向量索引；只处理派生 retrieval unit，原文 passage 永远不会被替代。
			</p>
			<div class="mt-3 flex flex-wrap gap-2">
				<button
					class="rounded bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50"
					disabled={busy || !selectedVersionId}
					on:click={() => indexVersion(false)}>索引未就绪项</button
				><button
					class="rounded border px-3 py-2 text-sm disabled:opacity-50"
					disabled={busy || !selectedVersionId}
					on:click={() => indexVersion(true)}>重建当前版本</button
				>
			</div>
			{#if versionIndexState}<div class="mt-3 rounded bg-gray-50 p-3 text-sm dark:bg-gray-800">
					<p>
						模式：{versionIndexState.mode === 'REBUILD' ? '重建全部' : '仅未就绪项'}；共 {versionIndexState.total_retrieval_units}
						项，本次 {versionIndexState.selected_retrieval_units} 项，就绪 {versionIndexState.ready}，降级
						{versionIndexState.degraded}，失败 {versionIndexState.failed}。
					</p>
					{#if versionIndexState.errors.length}<ul
							class="mt-2 list-disc space-y-1 pl-5 text-xs text-red-700 dark:text-red-300"
						>
							{#each versionIndexState.errors as error (error.retrieval_unit_id)}<li>
									<code>{error.retrieval_unit_id}</code>：{error.reason}
								</li>{/each}
						</ul>{/if}
				</div>{/if}
			<details class="mt-4 border-t pt-3">
				<summary class="cursor-pointer text-xs text-gray-500">按 retrieval unit ID 单项诊断</summary
				>
				<form class="mt-2" on:submit|preventDefault={indexUnit}>
					<label class="block text-sm"
						>Retrieval unit ID<input
							bind:value={retrievalUnitId}
							required
							class="mt-1 w-full rounded border bg-transparent px-2 py-1"
						/></label
					><button
						class="mt-3 rounded border px-3 py-2 text-sm disabled:opacity-50"
						disabled={busy || !retrievalUnitId.trim()}>建立单项索引</button
					>
				</form>
			</details>
		</section>
	</section>
</main>
