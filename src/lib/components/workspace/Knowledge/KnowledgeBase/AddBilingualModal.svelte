<script lang="ts">
	import { getContext } from 'svelte';
	import { toast } from 'svelte-sonner';
	import type { BilingualFile, BilingualEpubFile, BilingualImportData, LangCode } from '$lib/apis/retrieval';
	import { importBilingualGoogleSheet } from '$lib/apis/retrieval';
	import {LANG_LABELS, parseFileName} from '$lib/apis/knowledge';

	const i18n = getContext('i18n');

	export let show = false;
	export let onSubmit: (data: BilingualImportData, type: string) => void = () => {};
	export let onClose: () => void = () => {};
	export let collectionName: string | undefined = undefined;

	interface FilePair {
		baseName: string;
		files: Record<LangCode, File>;
	}

	interface ParagraphRow {
		index: number;
		langs: Record<LangCode, string>;
	}

	const getLangLabel = (code: string) => LANG_LABELS[code.toLowerCase()] ?? code.toUpperCase();



	type Step = 'select' | 'preview' | 'confirm';
	let step: Step = 'select';
	let importMode: 'files' | 'epub' | 'google' = 'files'; // 'files' / 'epub' / 'google'

	let filePairs: FilePair[] = [];
	let detectedLanguages: LangCode[] = [];
	let primaryLang: LangCode = 'zh';

	let paragraphRows: ParagraphRow[] = [];
	let previewPairIndex = 0;
	let isProcessing = false;
	let processingProgress = 0;
	let processingMessage = '';

	let alignWarnings: { baseName: string; counts: Record<LangCode, number> }[] = [];

	// ── Google Sheet 导入模式专用状态 ──────────────────────────────────────
	let sheetUrl = '';
	let skipFirstTableRow = false;
	// 后端 /google-sheet 接口返回的原始结果（逐段对齐好的多语言数据）
	let googleFiles: {
		id: string;
		baseName: string;
		languages: string[];
		paragraphs: { index: number; langs: Record<string, string> }[];
		primaryLang: string;
		primaryText: string;
		docLinks: Record<string, string>;
		errors: Record<string, string>;
	}[] = [];
	let googleRowErrors: { row: number; baseName: string; reason: string }[] = [];

	async function handleDirectorySelect() {
		try {
			let allFiles: File[] = [];
			allFiles = await selectViaInput();
			if (allFiles.length === 0) {
				toast.warning('No files found in the selected directory.');
				return;
			}

			await parseFilePairs(allFiles, true);
		} catch (e: any) {
			if (e?.name !== 'AbortError') {
				toast.error('Failed to read directory: ' + e?.message);
			}
		}
	}

	function selectViaInput(): Promise<File[]> {
		return new Promise((resolve, reject) => {
			const input = document.createElement('input');
			input.type = 'file';
			(input as any).webkitdirectory = true;
			input.multiple = true;
			input.style.display = 'none';
			document.body.appendChild(input);
			input.onchange = () => {
				document.body.removeChild(input);
				resolve(Array.from(input.files ?? []));
			};
			input.onerror = reject;
			input.click();
		});
	}

	async function handleEpubSelect() {
		try {
			let allFiles: File[] = [];
			allFiles = await selectViaInput();

			let filterFiles = allFiles.filter((file)=>{
				return file.name.toLowerCase().endsWith('.epub')
			}) 

			if (filterFiles.length === 0) {
				toast.warning('No epub files found in the selected directory.');
				return;
			}
			await parseFilePairs(filterFiles, true);
		} catch (e: any) {
			if (e?.name !== 'AbortError') {
				toast.error('Failed to select EPUB: ' + e?.message);
			}
		}
	}

	async function parseFilePairs(files: File[], is_show: boolean=true) {
		isProcessing = true;
		processingProgress = 0;
		processingMessage = 'Scanning files...';

		// 只处理文本类文件
		const textFiles = files.filter((f) => /\.(txt|md|html|xml|json|epub)$/i.test(f.name));

		const pairMap = new Map<string, Record<LangCode, File>>();
		const langSet = new Set<LangCode>();

		for (const file of textFiles) {
			const shortName = file.name.split('/').pop() ?? file.name;
			const parsed = parseFileName(shortName);
			if (!parsed) continue;

			const { base, lang } = parsed;
			if (!pairMap.has(base)) pairMap.set(base, {});
			pairMap.get(base)![lang] = file;
			langSet.add(lang);
		}

		// 只保留有 ≥2 种语言的配对
		const validPairs: FilePair[] = [];
		for (const [baseName, langFiles] of pairMap.entries()) {
			if (Object.keys(langFiles).length >= 2) {
				validPairs.push({ baseName, files: langFiles });
			}
		}

		if (validPairs.length === 0) {
			toast.error(
				'No bilingual file pairs found. Make sure files share a base name with language suffixes, e.g. article_zh.txt / article_en.txt'
			);
			isProcessing = false;
			return;
		}

		filePairs = validPairs;
		detectedLanguages = Array.from(langSet).sort();

		primaryLang = detectedLanguages.includes('zh') ? 'zh' : detectedLanguages[0];
		
		if (is_show){
			await loadParagraphPreview(0);
		}

		isProcessing = false;
		step = 'preview';
	}

	async function loadParagraphPreview(pairIndex: number) {
		previewPairIndex = pairIndex;

		// Google Sheet 模式：直接用后端已经抓取并逐段对齐好的段落数据展示，不需要再读文件
		if (importMode === 'google') {
			const gFile = googleFiles[pairIndex];
			if (!gFile) return;
			paragraphRows = gFile.paragraphs.map((p) => ({ index: p.index, langs: p.langs }));
			return;
		}

		const pair = filePairs[pairIndex];
		if (!pair) return;

		const langContents: Record<LangCode, string[]> = {};

		for (const [lang, file] of Object.entries(pair.files)) {
			let text;
			if(file.name.toLowerCase().endsWith('.txt')){
				text = await file.text();
			}else{
				text = "........";
			}
			langContents[lang] = [text];
		}

		paragraphRows = [
			{
				index: 0,
				langs: Object.fromEntries(
					Object.entries(langContents).map(([lang, contents]) => [lang, contents[0] ?? ''])
				)
			}
		];
	}

	async function handleGoogleSheetImport() {
		if (!sheetUrl.trim()) {
			toast.warning($i18n?.t('Please enter a Google Sheet link') ?? '请输入 Google 表格链接');
			return;
		}

		isProcessing = true;
		processingProgress = 10;
		processingMessage = $i18n?.t('Reading Google Sheet & documents...') ?? '正在读取表格与文档...';

		try {
			const result = await importBilingualGoogleSheet(
				localStorage.token,
				sheetUrl.trim(),
				undefined, 
				skipFirstTableRow
			);

			if (!result.files || result.files.length === 0) {
				toast.error($i18n?.t('No valid rows found in the sheet.') ?? '表格中没有解析到任何有效数据行。');
				isProcessing = false;
				return;
			}

			googleFiles = result.files;
			googleRowErrors = result.rowErrors ?? [];
			detectedLanguages = result.languages as LangCode[];
			primaryLang = result.primaryLang as LangCode;

			// 用已有的 alignWarnings 展示形式复用告警条（把 rowErrors 转成 counts 提示文案）
			alignWarnings = googleRowErrors.map((e) => ({
				baseName: e.baseName,
				counts: { info: e.reason } as any
			}));

			await loadParagraphPreview(0);

			processingProgress = 100;
			isProcessing = false;
			step = 'preview';
		} catch (e: any) {
			toast.error(($i18n?.t('Import failed') ?? '导入失败') + '：' + (e?.message ?? '未知错误'));
			isProcessing = false;
		}
	}

	async function handleConfirm() {
		isProcessing = true;
		processingProgress = 0;
		processingMessage = 'Processing paragraphs...';

		const files: BilingualFile[] = [];

		if (importMode === 'google') {
			// 把逐段对齐的 Google 数据，拼接还原成与 txt 模式一致的整篇文本结构
			// （各语言段落用 \n\n 连接，与 Step 3 里 "Paragraph Separator: Blank lines" 的约定保持一致，
			//  这样可以直接复用现有的 processBilingual 后端管道，无需额外改动）
			const total = googleFiles.length;
			for (let idx = 0; idx < googleFiles.length; idx++) {
				const gFile = googleFiles[idx];
				processingProgress = Math.round(((idx + 0.5) / total) * 50);

				const langs: Record<LangCode, string> = {};
				for (const lang of detectedLanguages) {
					langs[lang] = gFile.paragraphs.map((p) => p.langs[lang] ?? '').join('\n\n');
				}

				const primaryText = gFile.primaryText || langs[primaryLang] || '';
				if (!primaryText.trim()) continue;

				files.push({
					id: gFile.id,
					baseName: gFile.baseName,
					langs,
					primaryLang,
					primaryText
				});
				processingProgress = Math.round(((idx + 1) / total) * 50);
			}

			try {
				await onSubmit(
					{
						files,
						primaryLang,
						languages: detectedLanguages,
						totalFiles: total
					},
					'txt' // 复用现有 txt 模式的提交管道
				);

				isProcessing = false;
				processingProgress = 100;
				handleClose();
			} catch (e: any) {
				toast.error('导入失败：' + (e?.message ?? '未知错误'));
				isProcessing = false;
			}
			return;
		}

		const total = filePairs.length;

		for (let pairIdx = 0; pairIdx < filePairs.length; pairIdx++) {
			const pair = filePairs[pairIdx];
			processingProgress = Math.round(((pairIdx + 0.5) / total) * 50);

			const langs: Record<LangCode, string> = {};
			for (const [lang, file] of Object.entries(pair.files)) {
				langs[lang] = await file.text();
			}

			const primaryText = langs[primaryLang] ?? Object.values(langs).find(Boolean) ?? '';
			if (!primaryText.trim()) continue;

			files.push({
				id: `${pair.baseName}__p0`,
				baseName: pair.baseName,
				langs,
				primaryLang,
				primaryText,
			});
			processingProgress = Math.round(((pairIdx + 1) / total) * 50);
		}

		try {
			await onSubmit({
				files,
				primaryLang,
				languages: detectedLanguages,
				totalFiles: total
			}, "txt");

			isProcessing = false;
			processingProgress = 100;

			handleClose();
		} catch (e: any) {
			toast.error('导入失败：' + (e?.message ?? '未知错误'));
			isProcessing = false;
		}
	}

	async function handleEpubConfirm(){
		isProcessing = true;
		processingProgress = 0;
		processingMessage = 'Processing paragraphs...';

		const files: BilingualEpubFile[] = [];
		const total = filePairs.length;

		for (let pairIdx = 0; pairIdx < filePairs.length; pairIdx++) {
			const pair = filePairs[pairIdx];
			processingProgress = Math.round(((pairIdx + 0.5) / total) * 50);

			const langs: Record<LangCode, File> = {};
			for (const [lang, file] of Object.entries(pair.files)) {
				langs[lang] = file;
			}

			files.push({
				id: `${pair.baseName}__p0`,
				baseName: pair.baseName,
				langs,
				primaryLang,
			});
			processingProgress = Math.round(((pairIdx + 1) / total) * 50);
		}

		try {
			await onSubmit({
				files,
				primaryLang,
				languages: detectedLanguages,
				totalFiles: total
			}, "epub");

			isProcessing = false;
			processingProgress = 100;

			handleClose();
		} catch (e: any) {
			toast.error('导入失败：' + (e?.message ?? '未知错误'));
			isProcessing = false;
		}
	}

	function handleClose() {
		show = false;
		step = 'select';
		importMode = 'files';
		filePairs = [];
		paragraphRows = [];
		alignWarnings = [];
		sheetUrl = '';
		skipFirstTableRow = false;
		googleFiles = [];
		googleRowErrors = [];
		onClose();
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') handleClose();
	}
</script>

<svelte:window on:keydown={handleKeydown} />
{#if show}
	<!-- Backdrop -->
	<div
		class="fixed inset-0 z-50 flex items-center justify-center"
		role="dialog"
		aria-modal="true"
		aria-label="Bilingual Import"
	>
		<!-- Overlay -->
		<button
			class="absolute inset-0 bg-black/40 backdrop-blur-sm"
			on:click={handleClose}
			tabindex="-1"
			aria-label="Close"
		/>

		<!-- Modal -->
		<div
			class="relative z-10 w-full max-w-4xl mx-4 bg-white dark:bg-gray-900 rounded-2xl shadow-2xl border border-gray-200 dark:border-gray-700 flex flex-col max-h-[90vh]"
		>
			<!-- Header -->
			<div class="flex items-center justify-between px-6 py-4 border-b border-gray-100 dark:border-gray-800 shrink-0">
				<div class="flex items-center gap-3">
					<!-- Step indicator -->
					<div class="flex items-center gap-1.5">
						{#each ['select', 'preview', 'confirm'] as s, idx}
							<div class="flex items-center gap-1.5">
								<div
									class="w-6 h-6 rounded-full flex items-center justify-center text-xs font-semibold transition-colors
										{step === s
											? 'bg-blue-500 text-white'
											: ['select', 'preview', 'confirm'].indexOf(step) > idx
											? 'bg-green-500 text-white'
											: 'bg-gray-200 dark:bg-gray-700 text-gray-500'}"
								>
									{#if ['select', 'preview', 'confirm'].indexOf(step) > idx}
										✓
									{:else}
										{idx + 1}
									{/if}
								</div>
								{#if idx < 3}
									<div class="w-6 h-px bg-gray-200 dark:bg-gray-700" />
								{/if}
							</div>
						{/each}
					</div>

					<h2 class="text-base font-semibold text-gray-900 dark:text-white ml-2">
						{#if step === 'select'}
							{$i18n?.t('Import Bilingual Files') ?? 'Import Bilingual Files'}
						{:else if step === 'preview'}
							{$i18n?.t('Preview & Align') ?? 'Preview & Align'}
						{:else}
							{$i18n?.t('Confirm Import') ?? 'Confirm Import'}
						{/if}
					</h2>
				</div>

				<button
					class="p-1.5 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:bg-gray-800 transition"
					on:click={handleClose}
					aria-label="Close"
				>
					<svg xmlns="http://www.w3.org/2000/svg" class="w-5 h-5" viewBox="0 0 20 20" fill="currentColor">
						<path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" />
					</svg>
				</button>
			</div>

			<!-- Body -->
			<div class="flex-1 overflow-y-auto px-6 py-5">

				<!-- ── Step 1: Select Import Mode ── -->
				{#if step === 'select'}
					<div class="flex flex-col items-center justify-center gap-6 py-10">
						<h3 class="text-lg font-semibold text-gray-900 dark:text-white">
							{$i18n?.t('Choose Import Method') ?? 'Choose Import Method'}
						</h3>

						<div class="grid grid-cols-1 md:grid-cols-3 gap-4 w-full max-w-3xl">
							<!-- Files Option -->
							<button
								class="p-6 rounded-xl border-2 transition-all hover:border-blue-500 hover:bg-blue-50 dark:hover:bg-blue-900/20 {importMode === 'files' ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20' : 'border-gray-200 dark:border-gray-700'}"
								on:click={() => (importMode = 'files')}
							>
								<div class="flex flex-col items-center gap-3">
									<div class="w-16 h-16 rounded-2xl bg-blue-100 dark:bg-blue-900/40 flex items-center justify-center">
										<svg xmlns="http://www.w3.org/2000/svg" class="w-8 h-8 text-blue-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
											<path stroke-linecap="round" stroke-linejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
										</svg>
									</div>
									<div>
										<h4 class="font-semibold text-gray-900 dark:text-white">
											{$i18n?.t('Bilingual Files') ?? 'Bilingual Files'}
										</h4>
										<p class="text-xs text-gray-500 dark:text-gray-400 mt-1">
											{$i18n?.t('Import from paired text files') ?? 'Import from paired text files'}
										</p>
									</div>
								</div>
							</button>

							<!-- EPUB Option -->
							<button
								class="p-6 rounded-xl border-2 transition-all hover:border-purple-500 hover:bg-purple-50 dark:hover:bg-purple-900/20 {importMode === 'epub' ? 'border-purple-500 bg-purple-50 dark:bg-purple-900/20' : 'border-gray-200 dark:border-gray-700'}"
								on:click={() => (importMode = 'epub')}
							>
								<div class="flex flex-col items-center gap-3">
									<div class="w-16 h-16 rounded-2xl bg-purple-100 dark:bg-purple-900/40 flex items-center justify-center">
										<svg xmlns="http://www.w3.org/2000/svg" class="w-8 h-8 text-purple-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
											<path stroke-linecap="round" stroke-linejoin="round" d="M12 6.253v13m0-13C6.5 6.253 2 10.998 2 17s4.5 10.747 10 10.747c5.5 0 10-4.998 10-10.747S17.5 6.253 12 6.253z" />
										</svg>
									</div>
									<div>
										<h4 class="font-semibold text-gray-900 dark:text-white">
											{$i18n?.t('EPUB File') ?? 'EPUB File'}
										</h4>
										<p class="text-xs text-gray-500 dark:text-gray-400 mt-1">
											{$i18n?.t('Extract & import from EPUB') ?? 'Extract & import from EPUB'}
										</p>
									</div>
								</div>
							</button>

							<!-- Google Sheet Option -->
							<button
								class="p-6 rounded-xl border-2 transition-all hover:border-emerald-500 hover:bg-emerald-50 dark:hover:bg-emerald-900/20 {importMode === 'google' ? 'border-emerald-500 bg-emerald-50 dark:bg-emerald-900/20' : 'border-gray-200 dark:border-gray-700'}"
								on:click={() => (importMode = 'google')}
							>
								<div class="flex flex-col items-center gap-3">
									<div class="w-16 h-16 rounded-2xl bg-emerald-100 dark:bg-emerald-900/40 flex items-center justify-center">
										<svg xmlns="http://www.w3.org/2000/svg" class="w-8 h-8 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
											<path stroke-linecap="round" stroke-linejoin="round" d="M8 7h8m-8 4h8m-8 4h5M5 3h14a2 2 0 012 2v14a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2z" />
										</svg>
									</div>
									<div>
										<h4 class="font-semibold text-gray-900 dark:text-white">
											{$i18n?.t('Google Sheet') ?? 'Google Sheet'}
										</h4>
										<p class="text-xs text-gray-500 dark:text-gray-400 mt-1">
											{$i18n?.t('Import from linked Google Docs') ?? '从表格里的谷歌文档链接导入'}
										</p>
									</div>
								</div>
							</button>
						</div>

						{#if importMode !== 'google'}
							<button
								class="px-6 py-2.5 bg-blue-500 hover:bg-blue-600 text-white font-medium text-sm rounded-xl transition flex items-center gap-2"
								on:click={() => {
									if (importMode === 'files') {
										handleDirectorySelect();
									} else {
										handleEpubSelect();
									}
								}}
								disabled={isProcessing}
							>
								{#if isProcessing}
									<svg class="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
										<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
										<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/>
									</svg>
									{processingMessage}
								{:else}
									<svg xmlns="http://www.w3.org/2000/svg" class="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
										<path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
									</svg>
									{importMode === 'files' ? 'Select Directory' : 'Select EPUB File'}
								{/if}
							</button>
						{/if}

						{#if importMode === 'files'}
							<div class="w-full max-w-md bg-gray-50 dark:bg-gray-800 rounded-xl p-4">
								<div class="grid grid-cols-2 gap-2">
									{#each [
										['article_zh.txt', 'article_en.txt'],
										['chapter-zh.md', 'chapter-en.md'],
									] as [a, b]}
										<div class="flex flex-col gap-1">
											<div class="text-xs font-mono bg-white dark:bg-gray-750 border border-gray-200 dark:border-gray-700 rounded px-2 py-1 text-gray-600 dark:text-gray-300">{a}</div>
											<div class="text-xs font-mono bg-white dark:bg-gray-750 border border-gray-200 dark:border-gray-700 rounded px-2 py-1 text-gray-600 dark:text-gray-300">{b}</div>
										</div>
									{/each}
								</div>
								<div class="mt-3 text-xs text-gray-400 dark:text-gray-500">
									Supports: .txt .md .html .xml .json
								</div>
							</div>
						{:else if importMode === 'epub'}
							<div class="w-full max-w-md bg-purple-50 dark:bg-purple-900/20 rounded-xl p-4 border border-purple-200 dark:border-purple-700">
								<div class="flex gap-2 mb-2">
									<svg class="w-4 h-4 text-purple-600 dark:text-purple-400 mt-0.5 shrink-0" fill="currentColor" viewBox="0 0 20 20">
										<path fill-rule="evenodd" d="M18 5v8a2 2 0 01-2 2h-5l-5 4v-4H4a2 2 0 01-2-2V5a2 2 0 012-2h12a2 2 0 012 2z" clip-rule="evenodd"/>
									</svg>
									<div class="text-xs text-purple-700 dark:text-purple-300">
										<strong>EPUB Processing:</strong> Your EPUB will be automatically split into chapters by the server.
									</div>
								</div>
								<div class="text-xs text-purple-600 dark:text-purple-400 space-y-1">
									<p>✓ Supports bilingual EPUB files</p>
									<p>✓ Extracts chapters automatically</p>
									<p>✓ Aligns parallel texts</p>
								</div>
							</div>
						{:else if importMode === 'google'}
							<div class="w-full max-w-md flex flex-col gap-3">
								<div class="bg-emerald-50 dark:bg-emerald-900/20 rounded-xl p-4 border border-emerald-200 dark:border-emerald-700">
									<div class="flex gap-2 mb-2">
										<svg class="w-4 h-4 text-emerald-600 dark:text-emerald-400 mt-0.5 shrink-0" fill="currentColor" viewBox="0 0 20 20">
											<path fill-rule="evenodd" d="M4 4a2 2 0 012-2h8a2 2 0 012 2v12a2 2 0 01-2 2H6a2 2 0 01-2-2V4zm2 2v2h8V6H6zm0 4v2h8v-2H6zm0 4v2h4v-2H6z" clip-rule="evenodd"/>
										</svg>
										<div class="text-xs text-emerald-700 dark:text-emerald-300">
											{$i18n?.t('Sheet header cells must use "source-target" language pairs, e.g. zh-ja, zh-en. Each cell in a row is a link to a Google Doc containing a table (left column = original text, right column = translation), aligned row by row.') ?? '表格表头需为"源语言-目标语言"格式，如 zh-ja、zh-en。每个单元格是一个 Google 文档链接，文档内为表格（左列原文/右列译文），逐行对齐。'}
										</div>
									</div>
									<div class="text-xs text-emerald-600 dark:text-emerald-400 space-y-1">
										<p>✓ {$i18n?.t('Sheet and docs must be shared as "Anyone with the link"') ?? '表格和文档都需公开分享（知道链接的任何人可查看）'}</p>
										<p>✓ {$i18n?.t('Automatically fetches and aligns each row') ?? '自动抓取并按行对齐每一段'}</p>
									</div>
								</div>

								<input
									type="text"
									class="w-full text-sm px-3 py-2 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-200 outline-none focus:border-emerald-500"
									bind:value={sheetUrl}
									placeholder={$i18n?.t('Paste Google Sheet share link') ?? '粘贴 Google 表格分享链接'}
									disabled={isProcessing}
								/>

								<!-- <label class="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
									<input type="checkbox" bind:checked={skipFirstTableRow} disabled={isProcessing} />
									{$i18n?.t('Skip the first row of each doc table (e.g. an "Original | Translation" header row)') ?? '跳过每个文档表格的第一行（如"原文｜译文"这样的说明性表头）'}
								</label> -->

								<button
									class="px-6 py-2.5 bg-emerald-500 hover:bg-emerald-600 text-white font-medium text-sm rounded-xl transition flex items-center justify-center gap-2"
									on:click={handleGoogleSheetImport}
									disabled={isProcessing}
								>
									{#if isProcessing}
										<svg class="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
											<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
											<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/>
										</svg>
										{processingMessage}
									{:else}
										{$i18n?.t('Fetch & Import') ?? '抓取并导入'}
									{/if}
								</button>
							</div>
						{/if}
					</div>

				<!-- ── Step 2: Preview ── -->
				{:else if step === 'preview'}
					<!-- File pairs summary -->
					<div class="flex items-center gap-3 mb-4 flex-wrap">
						<div class="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
							<span class="font-medium text-gray-700 dark:text-gray-200">
								{importMode === 'google' ? googleFiles.length : filePairs.length}
							</span>file pairs detected
						</div>
						<div class="flex gap-1.5 flex-wrap">
							{#each detectedLanguages as lang}
								<span class="px-2 py-0.5 text-xs rounded-full font-medium
									{lang === primaryLang
										? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300'
										: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300'}">
									{getLangLabel(lang)}
									{#if lang === primaryLang}<span class="ml-1 opacity-60">(primary)</span>{/if}
								</span>
							{/each}
						</div>

						<!-- Primary lang selector -->
						<div class="ml-auto flex items-center gap-2 text-sm">
							<span class="text-gray-500 dark:text-gray-400">Embedding language:</span>
							<select
								bind:value={primaryLang}
								class="text-sm px-2 py-1 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-200 outline-none"
							>
								{#each detectedLanguages as lang}
									<option value={lang}>{getLangLabel(lang)}</option>
								{/each}
							</select>
						</div>
					</div>

					<!-- Alignment warnings -->
					{#if alignWarnings.length > 0}
						{#each alignWarnings as w}
							<div class="mb-3 flex items-start gap-2 px-3 py-2.5 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700 rounded-xl text-sm text-amber-700 dark:text-amber-300">
								<svg class="w-4 h-4 mt-0.5 shrink-0" fill="currentColor" viewBox="0 0 20 20">
									<path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/>
								</svg>
								<span>
									<strong>{w.baseName}</strong>:
									{#if importMode === 'google'}
										{(w.counts as any)?.info}
									{:else}
										paragraph counts differ —
										{Object.entries(w.counts).map(([l, n]) => `${getLangLabel(l)}: ${n}`).join(', ')}.
										Mismatched positions will be left empty.
									{/if}
								</span>
							</div>
						{/each}
					{/if}

					<!-- File pair selector (if multiple) -->
					{#if (importMode === 'google' ? googleFiles.length : filePairs.length) > 1}
						<div class="flex gap-1.5 mb-3 overflow-x-auto pb-1 scrollbar-none">
							{#each (importMode === 'google' ? googleFiles : filePairs) as pair, idx}
								<button
									class="shrink-0 px-3 py-1 text-xs rounded-lg font-medium transition
										{previewPairIndex === idx
											? 'bg-blue-500 text-white'
											: 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700'}"
									on:click={() => loadParagraphPreview(idx)}
								>
									{pair.baseName}
								</button>
							{/each}
						</div>
					{/if}

					<!-- Paragraph alignment table -->
					<div class="border border-gray-200 dark:border-gray-700 rounded-xl overflow-hidden">
						<!-- Table header -->
						<div
							class="grid gap-px bg-gray-200 dark:bg-gray-700"
							style="grid-template-columns: 2.5rem repeat({detectedLanguages.length}, 1fr)"
						>
							<div class="bg-gray-50 dark:bg-gray-800 px-2 py-2 text-xs font-medium text-gray-400 text-center">#</div>
							{#each detectedLanguages as lang}
								<div class="bg-gray-50 dark:bg-gray-800 px-3 py-2 text-xs font-semibold text-gray-600 dark:text-gray-300 flex items-center gap-1.5">
									{#if lang === primaryLang}
										<span class="w-1.5 h-1.5 rounded-full bg-blue-500 shrink-0" />
									{/if}
									{getLangLabel(lang)}
									<span class="text-gray-400 font-normal ml-auto">{lang}</span>
								</div>
							{/each}
						</div>

						<!-- Rows -->
						<div class="max-h-64 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-800">
							{#each paragraphRows.slice(0, 30) as row}
								<div
									class="grid gap-px bg-gray-100 dark:bg-gray-800 hover:bg-blue-50 dark:hover:bg-blue-900/10 transition-colors"
									style="grid-template-columns: 2.5rem repeat({detectedLanguages.length}, 1fr)"
								>
									<div class="bg-white dark:bg-gray-900 px-2 py-2 text-xs text-gray-400 text-center flex items-start justify-center pt-3">
										{row.index + 1}
									</div>
									{#each detectedLanguages as lang}
										<div class="bg-white dark:bg-gray-900 px-3 py-2 text-xs text-gray-700 dark:text-gray-300 leading-relaxed">
											{#if row.langs[lang]}
												{row.langs[lang].length > 120 ? row.langs[lang].slice(0, 120) + '…' : row.langs[lang]}
											{:else}
												<span class="text-gray-300 dark:text-gray-600 italic">— empty —</span>
											{/if}
										</div>
									{/each}
								</div>
							{/each}
							{#if paragraphRows.length > 30}
								<div class="py-2 text-center text-xs text-gray-400">
									... and {paragraphRows.length - 30} more paragraphs
								</div>
							{/if}
						</div>
					</div>

					<div class="mt-3 text-xs text-gray-400 dark:text-gray-500">
						Showing preview for
						<strong class="text-gray-500">
							{(importMode === 'google' ? googleFiles : filePairs)[previewPairIndex]?.baseName}
						</strong>.
						Total across all pairs will be computed on import.
					</div>

				<!-- ── Step 3: Confirm ── -->
				{:else if step === 'confirm'}
					<div class="space-y-4">
						<div class="grid grid-cols-2 gap-3">
							{#each [
								{ label: 'File Pairs', value: importMode === 'google' ? googleFiles.length : filePairs.length },
								{ label: 'Languages', value: detectedLanguages.map(getLangLabel).join(' · ') },
								{ label: 'Primary Language (Embedding)', value: getLangLabel(primaryLang) },
								{ label: 'Paragraph Separator', value: 'Blank lines (\\n\\n)' },
							] as item}
								<div class="bg-gray-50 dark:bg-gray-800 rounded-xl px-4 py-3">
									<div class="text-xs text-gray-400 dark:text-gray-500 mb-1">{item.label}</div>
									<div class="text-sm font-medium text-gray-800 dark:text-gray-100">{item.value}</div>
								</div>
							{/each}
						</div>

						<div class="bg-blue-50 dark:bg-blue-900/20 border border-blue-100 dark:border-blue-800 rounded-xl px-4 py-3 text-sm text-blue-700 dark:text-blue-300">
							<strong>How it works:</strong> Each paragraph pair becomes one chunk in the knowledge base.
							The <strong>{getLangLabel(primaryLang)}</strong> text will be embedded for semantic search.
							All language versions are stored as metadata, so retrieval returns the full translation set.
						</div>

						{#if isProcessing}
							<div class="space-y-2">
								<div class="flex justify-between text-xs text-gray-500">
									<span>Processing paragraphs...</span>
									<span>{processingProgress}%</span>
								</div>
								<div class="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-1.5">
									<div
										class="bg-blue-500 h-1.5 rounded-full transition-all"
										style="width: {processingProgress}%"
									/>
								</div>
							</div>
						{/if}
					</div>
				{/if}
			</div>

			<!-- Footer -->
			<div class="flex items-center justify-between px-6 py-4 border-t border-gray-100 dark:border-gray-800 shrink-0">
				<button
					class="px-4 py-2 text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition disabled:opacity-50"
					on:click={() => {
						if (step === 'select') handleClose();
						else if (step === 'preview') { 
							step = 'select'; 
							filePairs = []; 
							googleFiles = [];
							alignWarnings = [];
						}
						else if (step === 'confirm') {
							step = 'preview';
						}
					}}
					disabled={isProcessing}
				>
					{step === 'select' ? '取消' : '后退'}
				</button>

				<div class="flex gap-2">
					{#if step === 'preview'}
						<button
							class="px-5 py-2 bg-blue-500 hover:bg-blue-600 text-white font-medium text-sm rounded-xl transition disabled:opacity-50"
							on:click={() => { step = 'confirm'; }}
							disabled={(importMode === 'google' ? googleFiles.length : filePairs.length) === 0}
						>
							Review Import →
						</button>
					{:else if step === 'confirm'}
						<button
							class="px-5 py-2 bg-green-500 hover:bg-green-600 text-white font-medium text-sm rounded-xl transition disabled:opacity-50 flex items-center gap-2"
							on:click={() => {
								if (importMode === "files" || importMode === "google")
									handleConfirm()
								else
									handleEpubConfirm()
							}}
							disabled={isProcessing}
						>
							{#if isProcessing}
								<svg class="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
									<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
									<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/>
								</svg>
							{/if}
							Confirm & Import
						</button>
					{/if}
				</div>
			</div>
		</div>
	</div>
{/if}