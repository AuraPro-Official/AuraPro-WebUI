<script lang="ts">
	import { getContext, onMount, tick } from 'svelte';
	import { page } from '$app/stores';
	import { toast } from 'svelte-sonner';
	import {
		getBilingualAlign,
		updateSentenceTranslation,
		getBilingualWords,
		updateBilingualWords,
		type GetBilingualAlignResponse,
		type SentenceAlignItem,
		type GetBilingualWordsResponse,
		type GlossaryTerm
	} from '$lib/apis/knowledge';

	const i18n = getContext('i18n');

	export let token: string;

	const bilingualId = $page.params.bilingualId;
	const collectionName = $page.url.searchParams.get('collection') ?? '';

	let data: GetBilingualAlignResponse | null = null;
	let loading = true;
	let errorMsg = '';

	let activeSentence: SentenceAlignItem | null = null;
	let activeAnchorEl: HTMLElement | null = null;
	let pendingCloseOnly = false;

	let popupTop = 0;
	let popupLeft = 0;

	let editingLang: string | null = null;
	let editingText = '';
	let savingLang: string | null = null;

	let pendingSentence: { sentence: SentenceAlignItem; anchor: HTMLElement } | null = null;
	let paraRefs: Record<number, HTMLElement> = {};

	// ── 术语侧边栏状态（可显示/隐藏，不影响阅读区布局） ─────────────────────
	let showGlossarySidebar = false;
	let glossaryLoaded = false;
	let glossaryData: GetBilingualWordsResponse | null = null;
	let glossaryLoading = false;
	let glossaryErrorMsg = '';
	// 每种语言一份可编辑的本地副本，保存前不影响原始数据
	let editableTerms: Record<string, GlossaryTerm[]> = {};
	let dirtyLangs: Record<string, boolean> = {};
	let glossarySavingLang: string | null = null;
	let termIdCounter = 0;

	onMount(async () => {
		try {
			data = await getBilingualAlign(token, bilingualId, collectionName);
		} catch (e: any) {
			errorMsg = e?.message ?? String(e);
		} finally {
			loading = false;
		}
	});

	const toggleGlossarySidebar = async () => {
		showGlossarySidebar = !showGlossarySidebar;
		if (showGlossarySidebar && !glossaryLoaded) {
			await loadGlossary();
		}
	};

	const loadGlossary = async () => {
		glossaryLoading = true;
		glossaryErrorMsg = '';
		try {
			const res = await getBilingualWords(token, bilingualId, collectionName);
			console.log('Glossary API Response:', res); // 关键：检查这里是否有 terms 数据

			if (!res || !res.terms) {
				throw new Error('No terms data received');
			}

			glossaryData = res;

			const newEditableTerms: Record<string, GlossaryTerm[]> = {};
			res.languages.forEach((lang) => {
				if (lang !== res.primary_lang) {
					newEditableTerms[lang] = res.terms[lang] ? res.terms[lang].map((t) => ({ ...t })) : [];
				}
			});

			editableTerms = newEditableTerms;
			dirtyLangs = {};
			glossaryLoaded = true;
		} catch (e: any) {
			console.error('Load Glossary Error:', e);
			glossaryErrorMsg = e?.message ?? String(e);
		} finally {
			glossaryLoading = false;
		}
	};

	const markDirty = (lang: string) => {
		dirtyLangs = { ...dirtyLangs, [lang]: true };
	};

	const addTermRow = (lang: string) => {
		termIdCounter += 1;
		editableTerms[lang] = [
			...(editableTerms[lang] ?? []),
			{ id: `new:${termIdCounter}`, lang, source: '', target: '' }
		];
		markDirty(lang);
	};

	const removeTermRow = (lang: string, idx: number) => {
		editableTerms[lang] = editableTerms[lang].filter((_, i) => i !== idx);
		markDirty(lang);
	};

	const saveGlossaryLang = async (lang: string) => {
		if (!editableTerms[lang]) return;
		glossarySavingLang = lang;
		try {
			const terms = editableTerms[lang]
				.filter((t) => t.source.trim() || t.target.trim())
				.map((t) => ({ source: t.source.trim(), target: t.target.trim() }));

			const res = await updateBilingualWords(token, {
				collection_name: collectionName,
				bilingual_id: bilingualId,
				lang,
				terms
			});

			toast.success(
				$i18n?.t('Saved {{count}} terms', { count: res.term_count }) ??
					`已保存 ${res.term_count} 条术语`
			);
			dirtyLangs = { ...dirtyLangs, [lang]: false };
		} catch (e: any) {
			toast.error(e?.message ?? $i18n?.t('Save failed') ?? '保存失败');
		} finally {
			glossarySavingLang = null;
		}
	};

	const hasUnsavedEdit = () => editingLang !== null;

	const closePopup = () => {
		activeSentence = null;
		activeAnchorEl = null;
		editingLang = null;
		editingText = '';
	};

	const reallyOpenSentence = async (sentence: SentenceAlignItem, anchorEl: HTMLElement) => {
		activeSentence = sentence;
		activeAnchorEl = anchorEl;
		editingLang = null;
		editingText = '';

		await tick();
		positionPopup(anchorEl);
	};

	const openSentence = (sentence: SentenceAlignItem, anchorEl: HTMLElement) => {
		if (activeSentence?.id === sentence.id) return;

		if (hasUnsavedEdit()) {
			pendingSentence = { sentence, anchor: anchorEl };
			return;
		}
		reallyOpenSentence(sentence, anchorEl);
	};

	const positionPopup = (anchorEl: HTMLElement) => {
		const rect = anchorEl.getBoundingClientRect();
		const containerRect = document.getElementById('align-reader-root')?.getBoundingClientRect();
		const offsetTop = containerRect ? containerRect.top : 0;
		const offsetLeft = containerRect ? containerRect.left : 0;

		popupTop = rect.bottom - offsetTop + 6;
		popupLeft = Math.max(0, rect.left - offsetLeft);
	};

	const startEdit = (lang: string) => {
		if (!activeSentence) return;
		editingLang = lang;
		editingText = activeSentence.langs[lang] ?? '';
	};

	const cancelEdit = () => {
		editingLang = null;
		editingText = '';
	};

	const saveEdit = async () => {
		if (!activeSentence || editingLang === null) return;
		const lang = editingLang;
		savingLang = lang;

		try {
			const res = await updateSentenceTranslation(token, {
				collection_name: collectionName,
				align_group_id: activeSentence.align_group_id,
				lang,
				text: editingText
			});

			activeSentence.langs[lang] = editingText;
			activeSentence.langs_modified = {
				...activeSentence.langs_modified,
				[lang]: res.langs_modified
			};
			data = data;

			toast.success($i18n?.t('Saved') ?? '已保存');
			editingLang = null;
			editingText = '';
		} catch (e: any) {
			toast.error(e?.message ?? $i18n?.t('Save failed') ?? '保存失败');
		} finally {
			savingLang = null;
		}
	};

	const renderParaSegments = (paraText: string, sentences: SentenceAlignItem[]) => {
		const segments: { text: string; sentence: SentenceAlignItem | null }[] = [];
		let cursor = 0;
		for (const s of sentences) {
			if (s.start > cursor) {
				segments.push({ text: paraText.slice(cursor, s.start), sentence: null });
			}
			segments.push({ text: paraText.slice(s.start, s.end), sentence: s });
			cursor = s.end;
		}
		if (cursor < paraText.length) {
			segments.push({ text: paraText.slice(cursor), sentence: null });
		}
		return segments;
	};

	const handleClickOutside = (e: MouseEvent) => {
		const root = document.getElementById('align-reader-root');
		const popup = document.getElementById('align-popup');
		if (!root) return;
		const target = e.target as Node;
		if (popup?.contains(target)) return;
		if (root.contains(target) && (target as HTMLElement).closest('[data-sentence-id]')) return;
		// 点击空白处关闭
		if (activeSentence) {
			if (hasUnsavedEdit()) {
				pendingSentence = null;
				pendingCloseOnly = true;
			} else {
				closePopup();
			}
		}
	};
</script>

<svelte:window on:click={handleClickOutside} />

<div class="p-4 max-w-6xl mx-auto">
	{#if loading}
		<div class="text-sm text-gray-400 py-10 text-center">
			{$i18n?.t('Loading...') ?? '加载中...'}
		</div>
	{:else if errorMsg}
		<div class="text-sm text-red-500 py-10 text-center">{errorMsg}</div>
	{:else if data}
		<div class="flex items-center justify-between mb-4">
			<h2 class="text-base font-medium text-gray-700 dark:text-gray-200">
				{data.bilingual_id} · {$i18n?.t('Primary') ?? '基准语言'}: {data.primary_lang}
			</h2>

			<!-- 术语侧边栏 显示/隐藏 切换按钮 -->
			<button
				class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg transition
                    {showGlossarySidebar
					? 'bg-blue-500 text-white hover:bg-blue-600'
					: 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700'}"
				on:click={toggleGlossarySidebar}
			>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					class="w-3.5 h-3.5"
					viewBox="0 0 20 20"
					fill="currentColor"
				>
					<path
						fill-rule="evenodd"
						d="M4 4a2 2 0 012-2h8a2 2 0 012 2v12a2 2 0 01-2 2H6a2 2 0 01-2-2V4zm2 2v2h8V6H6zm0 4v2h8v-2H6zm0 4v2h4v-2H6z"
						clip-rule="evenodd"
					/>
				</svg>
				{showGlossarySidebar
					? ($i18n?.t('Hide Glossary') ?? '隐藏术语')
					: ($i18n?.t('Show Glossary') ?? '显示术语')}
			</button>
		</div>

		<!-- 主体：阅读区 + 可隐藏的术语侧边栏 并排布局 -->
		<div class="flex gap-5 items-start">
			<!-- 对照阅读区 -->
			<div
				id="align-reader-root"
				class="relative leading-8 text-[15px] text-gray-800 dark:text-gray-200 space-y-4 flex-1 min-w-0 transition-all"
			>
				{#each data.paragraphs as para}
					<p bind:this={paraRefs[para.para_index]} class="whitespace-pre-wrap">
						{#each renderParaSegments(para.para_text, para.sentences) as seg}
							{#if seg.sentence}
								<span
									data-sentence-id={seg.sentence.id}
									class="cursor-pointer rounded transition-colors
                                           {activeSentence?.id === seg.sentence.id
										? 'bg-yellow-200/70 dark:bg-yellow-500/30'
										: 'hover:bg-yellow-100/60 dark:hover:bg-yellow-500/15'}"
									on:click={(e) => openSentence(seg.sentence!, e.currentTarget)}>{seg.text}</span
								>
							{:else}
								<span>{seg.text}</span>
							{/if}
						{/each}
					</p>
				{/each}

				<!-- Tooltip 浮窗 -->
				{#if activeSentence}
					<div
						id="align-popup"
						class="absolute z-50 w-[800px] max-w-[90vw] rounded-2xl
                            border border-gray-100 dark:border-gray-700
                            bg-white/95 dark:bg-gray-900/95
                            backdrop-blur-md shadow-2xl dark:shadow-black
                            ring-1 ring-black dark:ring-white/10
                            p-5 space-y-3"
						style="top: {popupTop}px; left: {popupLeft}px;"
					>
						{#each data.languages.filter((l) => l !== data?.primary_lang) as lang}
							{@const isEditing = editingLang === lang}
							{@const modified = activeSentence.langs_modified?.[lang]}
							<div
								class="border-b last:border-b-0 border-gray-100 dark:border-gray-800 pb-2 last:pb-0"
							>
								<div class="flex items-center justify-between mb-1">
									<span
										class="text-[11px] font-medium text-gray-400 dark:text-gray-500 flex items-center gap-1"
									>
										{lang.toUpperCase()}
										{#if modified}
											<span
												class="text-[10px] px-1 rounded bg-green-100 text-green-600 dark:bg-green-900/40 dark:text-green-300"
											>
												{$i18n?.t('Edited') ?? '已修改'}
											</span>
										{/if}
									</span>
									{#if !isEditing}
										<button
											class="text-[11px] text-blue-500 hover:underline"
											on:click={() => startEdit(lang)}
										>
											{$i18n?.t('Edit') ?? '编辑'}
										</button>
									{/if}
								</div>

								{#if isEditing}
									<textarea
										class="w-full text-sm rounded border border-gray-200 dark:border-gray-700
                                               bg-gray-50 dark:bg-gray-800 p-2 resize-none focus:outline-none
                                               focus:ring-1 focus:ring-blue-400"
										rows="3"
										bind:value={editingText}
									/>
									<div class="flex justify-end gap-2 mt-1.5">
										<button
											class="text-xs px-2 py-1 rounded text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
											on:click={cancelEdit}
										>
											{$i18n?.t('Cancel') ?? '取消'}
										</button>
										<button
											class="text-xs px-2 py-1 rounded bg-blue-500 text-white hover:bg-blue-600 disabled:opacity-50"
											disabled={savingLang === lang}
											on:click={saveEdit}
										>
											{savingLang === lang
												? ($i18n?.t('Saving...') ?? '保存中...')
												: ($i18n?.t('Save') ?? '保存')}
										</button>
									</div>
								{:else}
									<p class="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
										{activeSentence.langs[lang] || '—'}
									</p>
								{/if}
							</div>
						{/each}
					</div>
				{/if}
			</div>

			<!-- ── 术语侧边栏（可隐藏） ── -->
			{#if showGlossarySidebar}
				<div
					class="w-[360px] shrink-0 sticky top-4 max-h-[calc(100vh-6rem)] overflow-y-auto rounded-2xl border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 shadow-sm"
				>
					<div
						class="flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-800 sticky top-0 bg-white dark:bg-gray-900 z-10"
					>
						<div class="text-sm font-medium text-gray-700 dark:text-gray-200">
							{$i18n?.t('Glossary') ?? '术语列表'}
						</div>
						<button
							class="p-1 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
							title={$i18n?.t('Hide') ?? '隐藏'}
							on:click={() => (showGlossarySidebar = false)}
						>
							<svg
								xmlns="http://www.w3.org/2000/svg"
								class="w-4 h-4"
								viewBox="0 0 20 20"
								fill="currentColor"
							>
								<path
									fill-rule="evenodd"
									d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z"
									clip-rule="evenodd"
								/>
							</svg>
						</button>
					</div>

					<div class="p-3">
						{#if glossaryLoading}
							<div class="text-xs text-gray-400 py-8 text-center">
								{$i18n?.t('Loading...') ?? '加载中...'}
							</div>
						{:else if glossaryErrorMsg}
							<div class="text-xs text-red-500 py-8 text-center">{glossaryErrorMsg}</div>
						{:else if glossaryData}
							<div class="text-[11px] text-gray-400 dark:text-gray-500 mb-3 leading-relaxed">
								{$i18n?.t(
									'Terms are automatically extracted from aligned sentence pairs. You can review and correct them here — edits are saved to this file only.'
								) ?? '术语从对齐的句对中自动抽取，可在此核对修改；修改仅保存到当前文件。'}
							</div>

							{#each glossaryData.languages.filter((l) => l !== glossaryData?.primary_lang) as lang}
								<div
									class="mb-4 rounded-xl border border-gray-100 dark:border-gray-800 overflow-hidden"
								>
									<div
										class="flex items-center justify-between px-3 py-2 bg-gray-50 dark:bg-gray-850"
									>
										<div class="text-xs font-medium text-gray-700 dark:text-gray-200">
											{glossaryData.primary_lang.toUpperCase()} → {lang.toUpperCase()}
											<span class="ml-1 text-[10px] text-gray-400 font-normal">
												{(editableTerms[lang] ?? []).length}
												{$i18n?.t('terms') ?? '条'}
											</span>
										</div>
										<div class="flex items-center gap-1.5">
											{#if dirtyLangs[lang]}
												<span
													class="w-1.5 h-1.5 rounded-full bg-amber-400"
													title={$i18n?.t('Unsaved changes') ?? '有未保存的修改'}
												/>
											{/if}
											<button
												class="text-[11px] px-2 py-1 rounded-lg bg-blue-500 hover:bg-blue-600 text-white font-medium disabled:opacity-50 flex items-center gap-1"
												disabled={glossarySavingLang === lang || !dirtyLangs[lang]}
												on:click={() => saveGlossaryLang(lang)}
											>
												{#if glossarySavingLang === lang}
													<svg class="animate-spin w-3 h-3" viewBox="0 0 24 24" fill="none">
														<circle
															class="opacity-25"
															cx="12"
															cy="12"
															r="10"
															stroke="currentColor"
															stroke-width="4"
														/>
														<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z" />
													</svg>
												{/if}
												{glossarySavingLang === lang
													? ($i18n?.t('Saving...') ?? '保存中...')
													: ($i18n?.t('Save') ?? '保存')}
											</button>
										</div>
									</div>

									{#if (editableTerms[lang] ?? []).length === 0}
										<div class="px-3 py-4 text-center text-[11px] text-gray-400">
											{$i18n?.t('No terms extracted for this language yet.') ??
												'该语言暂无抽取出的术语。'}
										</div>
									{:else}
										<div class="divide-y divide-gray-50 dark:divide-gray-800">
											{#each editableTerms[lang] as term, idx (term.id)}
												<div
													class="flex items-center gap-1 px-2 py-1.5 hover:bg-gray-50/60 dark:hover:bg-gray-800/40 group"
												>
													<!-- Source 输入框 -->
													<div class="flex flex-1 items-center gap-1 min-w-0">
														<input
															type="text"
															class="w-full min-w-0 text-[11px] px-1.5 py-0.5 rounded border border-gray-200 dark:border-gray-700
                                                            bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-200 outline-none focus:border-blue-500"
															bind:value={term.source}
															on:input={() => markDirty(lang)}
															placeholder="Source"
														/>
													</div>

													<!-- 分隔箭头 (可选) -->
													<span class="text-gray-300 text-[10px]">→</span>

													<!-- Target 输入框 -->
													<div class="flex flex-1 items-center gap-1 min-w-0">
														<input
															type="text"
															class="w-full min-w-0 text-[11px] px-1.5 py-0.5 rounded border border-gray-200 dark:border-gray-700
                                                            bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-200 outline-none focus:border-blue-500"
															bind:value={term.target}
															on:input={() => markDirty(lang)}
															placeholder={lang.toUpperCase()}
														/>
													</div>

													<!-- 删除按钮 (悬停时显示) -->
													<button
														class="p-1 opacity-0 group-hover:opacity-100 text-gray-300 hover:text-red-500 transition shrink-0"
														on:click={() => removeTermRow(lang, idx)}
													>
														<svg
															xmlns="http://www.w3.org/2000/svg"
															class="w-3.5 h-3.5"
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
												</div>
											{/each}
										</div>
									{/if}

									<div class="px-3 py-2 border-t border-gray-50 dark:border-gray-800">
										<button
											class="text-[11px] text-blue-500 hover:underline flex items-center gap-1"
											on:click={() => addTermRow(lang)}
										>
											<svg
												xmlns="http://www.w3.org/2000/svg"
												class="w-3 h-3"
												viewBox="0 0 20 20"
												fill="currentColor"
											>
												<path
													fill-rule="evenodd"
													d="M10 5a1 1 0 011 1v3h3a1 1 0 110 2h-3v3a1 1 0 11-2 0v-3H6a1 1 0 110-2h3V6a1 1 0 011-1z"
													clip-rule="evenodd"
												/>
											</svg>
											{$i18n?.t('Add term') ?? '添加术语'}
										</button>
									</div>
								</div>
							{/each}
						{/if}
					</div>
				</div>
			{/if}
		</div>
	{/if}
</div>
