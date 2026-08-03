<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { showArchivedChats, showSidebar, mobile, user } from '$lib/stores';
	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import {
		activateGlossary,
		createGlossary,
		deleteGlossary,
		deleteGlossaryEntries,
		deleteGlossaryEntry,
		exportGlossary,
		getGlossary,
		getGlossarySettings,
		importGlossaryEntries,
		updateGlossarySettings,
		upsertGlossaryEntry
	} from '$lib/apis/glossary';

	import UserMenu from '$lib/components/layout/Sidebar/UserMenu.svelte';
	import SearchableSelect from '$lib/components/common/SearchableSelect.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Sidebar from '$lib/components/icons/Sidebar.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Search from '$lib/components/icons/Search.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import BookOpen from '$lib/components/icons/BookOpen.svelte';
	import Cog6 from '$lib/components/icons/Cog6.svelte';
	import DocumentArrowUp from '$lib/components/icons/DocumentArrowUp.svelte';

	const i18n = getContext('i18n');

	type GlossaryOrigin = 'user' | 'direct' | 'combined';

	type GlossaryEntry = {
		source: string;
		target: string;
		draftSource: string;
		draftTarget: string;
		dirty: boolean;
		origin: GlossaryOrigin;
		editingOverride: boolean;
	};

	type GlossaryRoute = {
		kind: 'direct' | 'combined';
		source_lang: string;
		target_lang: string;
		pivot_lang?: string | null;
		glossary_ids: string[];
		glossary_names: string[];
		coverage: number;
	};

	type GlossarySummary = {
		id: string;
		name?: string;
		source_lang?: string;
		target_lang?: string;
		glossary_lang?: string;
		official?: boolean;
		version?: string;
		path?: string;
	};

	type GlossarySettings = {
		active_glossary_id?: string;
		glossary_mode?: 'smart' | 'fixed';
		smart_source_lang?: string;
		smart_target_lang?: string;
		glossaries?: GlossarySummary[];
		glossary_lang?: string;
		glossary_path?: string;
		glossary_version?: string;
		max_terms_injected?: number;
		max_turns?: number;
		source_lang?: string;
		target_lang?: string;
		token_limit?: number;
	};

	type ImportHelpItem = {
		code?: string;
		bad?: string;
		reason?: string;
		good?: string;
	};

	type ImportHelpSection = {
		title: string;
		subtitle: string;
		type: 'default' | 'error';
		items: ImportHelpItem[];
	};

	let currentPage = 1;
	const pageSize = 50;

	let loading = true;
	let savingSettings = false;
	let importing = false;
	let query = '';
	let entries: GlossaryEntry[] = [];
	let settings: GlossarySettings = {};
	let glossaryRoutes: GlossaryRoute[] = [];
	let availableLanguages: string[] = [];
	let importText = '';
	let replaceImport = false;
	let importAsNewGlossary = false;
	let showImport = false;
	let showCreateGlossary = false;
	let showGlossarySettings = false;
	let glossaryFileInput: HTMLInputElement | null = null;
	let newGlossaryName = '';
	let newGlossarySourceLang = '';
	let newGlossaryTargetLang = '';
	let selectedEntries = new Set<string>();
	let confirmDialog: {
		show: boolean;
		title: string;
		message: string;
		confirmLabel: string;
		onConfirm: null | (() => Promise<void> | void);
	} = {
		show: false,
		title: '',
		message: '',
		confirmLabel: '',
		onConfirm: null
	};

	const importHelpSections: ImportHelpSection[] = [
		{
			title: '1. / 或者 | 别名分隔',
			subtitle: '两侧数量分为一对多和多对多（数量需相同）',
			type: 'default',
			items: [
				{ code: '"你好/嗨": "hello"\n→ 你好 → hello\n→ 嗨 → hello' },
				{ code: '"好呀|好啊": "yes|yeah"\n→ 好呀 → yes\n→ 好啊 → yeah' }
			]
		},
		{
			title: '2. 括号内 / 展开',
			subtitle: '括号内每个别名生成独立词条',
			type: 'default',
			items: [
				{ code: '"你好（呀/啊）": "hi"\n→ 你好呀 → hi\n→ 你好啊 → hi\n→ 你好 → hi' },
				{
					code: '"好（呀/啊）": "(yes/yeah/yesss)"\n→ 好呀 → [yes, yeah, yesss]\n→ 好啊 → [yes, yeah, yesss]'
				}
			]
		}
	];

	$: filteredEntries = entries.filter((entry) => {
		const needle = query.trim().toLowerCase();
		if (!needle) return true;
		return (
			entry.source.toLowerCase().includes(needle) || entry.target.toLowerCase().includes(needle)
		);
	});
	$: {
		filteredEntries;
		currentPage = 1;
	}
	$: totalPages = Math.ceil(filteredEntries.length / pageSize);
	$: pagedEntries = filteredEntries.slice((currentPage - 1) * pageSize, currentPage * pageSize);
	$: activeGlossary = (settings.glossaries ?? []).find(
		(glossary) => glossary.id === settings.active_glossary_id
	);
	$: activeGlossaryPair = `${activeGlossary?.source_lang ?? settings.source_lang ?? ''} -> ${
		activeGlossary?.target_lang ?? settings.target_lang ?? activeGlossary?.glossary_lang ?? ''
	}`;
	$: activeGlossaryVersion = activeGlossary?.version ?? settings.glossary_version ?? '1.0.0';

	const safeExportName = (value: string) =>
		(value || 'glossary')
			.trim()
			.replace(/[<>:"/\\|?*\x00-\x1f]+/g, '-')
			.replace(/^[\s.-]+|[\s.-]+$/g, '') || 'glossary';
	$: activeGlossaryPath = activeGlossary?.path ?? settings.glossary_path ?? '';
	$: activeGlossaryFile = getGlossaryFileName(activeGlossaryPath);
	$: selectableFilteredEntries = filteredEntries.filter(
		(entry) => entry.source && entry.origin === 'user'
	);
	$: allFilteredSelected =
		selectableFilteredEntries.length > 0 &&
		selectableFilteredEntries.every((entry) => selectedEntries.has(entry.source));

	const getGlossaryFileName = (path: string = '') => path.split(/[\\/]/).pop() ?? '';

	$: glossarySelectItems = (settings.glossaries ?? []).map((glossary) => {
		const source = glossary.source_lang ?? settings.source_lang ?? '';
		const target =
			glossary.target_lang ??
			glossary.glossary_lang ??
			settings.target_lang ??
			settings.glossary_lang ??
			'';
		const fileName = getGlossaryFileName(glossary.path ?? '');
		const label = `${source} → ${target}${glossary.official ? ` ${$i18n.t('Official')}` : ''} v${
			glossary.version ?? '1.0.0'
		}${fileName ? ` ${fileName}` : ''}`;

		return {
			value: glossary.id,
			label,
			searchTerms: [
				glossary.name,
				glossary.source_lang,
				glossary.target_lang,
				glossary.glossary_lang,
				glossary.version,
				glossary.path
			]
		};
	});

	$: isSmartMode = (settings.glossary_mode ?? 'smart') === 'smart';
	$: smartSourceLang = settings.smart_source_lang ?? settings.source_lang ?? '中文';
	$: smartTargetLang = settings.smart_target_lang ?? settings.target_lang ?? '外文';
	$: languageSelectItems = Array.from(
		new Set(
			[
				...availableLanguages,
				...(settings.glossaries ?? []).flatMap((glossary) => [
					glossary.source_lang,
					glossary.target_lang ?? glossary.glossary_lang
				]),
				smartSourceLang,
				smartTargetLang
			].filter((language): language is string => Boolean(language?.trim()))
		)
	)
		.sort((left, right) => left.localeCompare(right))
		.map((language) => ({ value: language, label: language }));

	const load = async () => {
		loading = true;
		try {
			const [glossary, glossarySettings] = await Promise.all([
				getGlossary(localStorage.token),
				getGlossarySettings(localStorage.token)
			]);
			entries = Object.entries(glossary?.entries ?? {})
				.map(([source, target]) => ({
					source,
					target: String(target),
					draftSource: source,
					draftTarget: String(target),
					dirty: false,
					origin: (glossary?.entry_origins?.[source] ?? 'user') as GlossaryOrigin,
					editingOverride: false
				}))
				.reverse();
			settings = glossarySettings ?? {};
			glossaryRoutes = glossary?.routes ?? [];
			availableLanguages = glossary?.languages ?? [];
			selectedEntries = new Set();
		} catch (error) {
			toast.error(`${error}`);
		}
		loading = false;
	};

	const isEntryEditable = (entry: GlossaryEntry) =>
		entry.origin === 'user' || entry.editingOverride;

	const beginPersonalCorrection = (entry: GlossaryEntry) => {
		entry.editingOverride = true;
		entries = entries;
	};

	const cancelPersonalCorrection = (entry: GlossaryEntry) => {
		entry.draftSource = entry.source;
		entry.draftTarget = entry.target;
		entry.dirty = false;
		entry.editingOverride = false;
		entries = entries;
	};

	const saveEntry = async (entry: GlossaryEntry) => {
		if (!isEntryEditable(entry)) return;
		const source = entry.draftSource.trim();
		const target = entry.draftTarget.trim();
		if (!source || !target) {
			toast.error($i18n.t('Both terms are required'));
			return;
		}

		try {
			if (entry.origin === 'user' && entry.source && entry.source !== source) {
				await deleteGlossaryEntry(localStorage.token, entry.source);
			}
			await upsertGlossaryEntry(localStorage.token, source, target);
			entry.source = source;
			entry.target = target;
			entry.dirty = false;
			toast.success($i18n.t('Saved'));
			await load();
		} catch (error) {
			toast.error(`${error}`);
		}
	};

	const addEntry = () => {
		entries = [
			{
				source: '',
				target: '',
				draftSource: '',
				draftTarget: '',
				dirty: true,
				origin: 'user',
				editingOverride: true
			},
			...entries
		];
	};

	const removeEntry = async (entry: GlossaryEntry) => {
		if (entry.origin !== 'user') return;
		if (!entry.source) {
			entries = entries.filter((item) => item !== entry);
			return;
		}
		try {
			await deleteGlossaryEntry(localStorage.token, entry.source);
			entries = entries.filter((item) => item !== entry);
			selectedEntries.delete(entry.source);
			selectedEntries = new Set(selectedEntries);
			toast.success($i18n.t('Deleted'));
			await load();
		} catch (error) {
			toast.error(`${error}`);
		}
	};

	const toggleEntrySelection = (source: string, checked: boolean) => {
		if (!source) return;
		if (checked) {
			selectedEntries.add(source);
		} else {
			selectedEntries.delete(source);
		}
		selectedEntries = new Set(selectedEntries);
	};

	const toggleFilteredSelection = (checked: boolean) => {
		const next = new Set(selectedEntries);
		for (const entry of selectableFilteredEntries) {
			if (checked) {
				next.add(entry.source);
			} else {
				next.delete(entry.source);
			}
		}
		selectedEntries = next;
	};

	const toggleFilteredSelectionFromEvent = (event: Event) => {
		toggleFilteredSelection((event.currentTarget as HTMLInputElement).checked);
	};

	const toggleEntrySelectionFromEvent = (source: string, event: Event) => {
		toggleEntrySelection(source, (event.currentTarget as HTMLInputElement).checked);
	};

	const askConfirm = (
		title: string,
		message: string,
		confirmLabel: string,
		onConfirm: () => Promise<void> | void
	) => {
		confirmDialog = {
			show: true,
			title,
			message,
			confirmLabel,
			onConfirm
		};
	};

	const runConfirmedAction = async () => {
		const action = confirmDialog.onConfirm;
		confirmDialog = { ...confirmDialog, show: false, onConfirm: null };
		await action?.();
	};

	const removeSelectedEntries = async () => {
		const sources = Array.from(selectedEntries).filter(Boolean);
		if (sources.length === 0) return;

		askConfirm(
			$i18n.t('Delete selected'),
			$i18n.t('Delete selected glossary entries?'),
			$i18n.t('Delete'),
			async () => {
				try {
					await deleteGlossaryEntries(localStorage.token, sources);
					selectedEntries = new Set();
					toast.success($i18n.t('Deleted'));
					await load();
				} catch (error) {
					toast.error(`${error}`);
				}
			}
		);
	};

	const saveSettings = async () => {
		savingSettings = true;
		try {
			settings = await updateGlossarySettings(localStorage.token, {
				glossary_mode: settings.glossary_mode ?? 'smart',
				smart_source_lang: settings.smart_source_lang,
				smart_target_lang: settings.smart_target_lang,
				glossary_path: settings.glossary_path,
				glossary_version: settings.glossary_version,
				source_lang: settings.source_lang,
				target_lang: settings.target_lang,
				glossary_lang: settings.target_lang,
				max_terms_injected: Number(settings.max_terms_injected || 10),
				max_turns: Number(settings.max_turns || 0),
				token_limit: Number(settings.token_limit || 0)
			});
			toast.success($i18n.t('Glossary settings updated'));
			await load();
		} catch (error) {
			toast.error(`${error}`);
		}
		savingSettings = false;
	};

	const updateGlossaryMode = async (mode: 'smart' | 'fixed') => {
		if ((settings.glossary_mode ?? 'smart') === mode) return;
		savingSettings = true;
		try {
			settings = await updateGlossarySettings(localStorage.token, {
				glossary_mode: mode,
				smart_source_lang: smartSourceLang,
				smart_target_lang: smartTargetLang
			});
			showGlossarySettings = false;
			await load();
		} catch (error) {
			toast.error(`${error}`);
		} finally {
			savingSettings = false;
		}
	};

	const updateSmartLanguage = async (side: 'source' | 'target', value: string) => {
		const language = value.trim();
		if (!language) return;
		const source = side === 'source' ? language : smartSourceLang;
		const target = side === 'target' ? language : smartTargetLang;
		if (source === smartSourceLang && target === smartTargetLang) return;

		savingSettings = true;
		try {
			settings = await updateGlossarySettings(localStorage.token, {
				glossary_mode: 'smart',
				smart_source_lang: source,
				smart_target_lang: target
			});
			await load();
		} catch (error) {
			toast.error(`${error}`);
		} finally {
			savingSettings = false;
		}
	};

	const switchGlossary = async (id: string) => {
		if (!id || id === settings.active_glossary_id) return;
		loading = true;
		try {
			settings = await activateGlossary(localStorage.token, id);
			showGlossarySettings = false;
			await load();
		} catch (error) {
			toast.error(`${error}`);
			loading = false;
		}
	};

	const createNewGlossary = async () => {
		const name = newGlossaryName.trim();
		const sourceLang =
			newGlossarySourceLang.trim() ||
			(isSmartMode ? smartSourceLang : settings.source_lang) ||
			'中文';
		const targetLang =
			newGlossaryTargetLang.trim() ||
			(isSmartMode ? smartTargetLang : settings.target_lang || settings.glossary_lang) ||
			'西班牙语';
		if (!sourceLang || !targetLang) return;
		loading = true;
		try {
			settings = await createGlossary(localStorage.token, name, sourceLang, targetLang, targetLang);
			newGlossaryName = '';
			newGlossarySourceLang = '';
			newGlossaryTargetLang = '';
			showCreateGlossary = false;
			await load();
		} catch (error) {
			toast.error(`${error}`);
			loading = false;
		}
	};

	const openCreateGlossaryModal = () => {
		newGlossaryName = '';
		newGlossarySourceLang = '';
		newGlossaryTargetLang = '';
		showCreateGlossary = true;
	};

	const closeCreateGlossaryModal = () => {
		newGlossaryName = '';
		newGlossarySourceLang = '';
		newGlossaryTargetLang = '';
		showCreateGlossary = false;
	};

	const removeActiveGlossary = async () => {
		if (!settings.active_glossary_id || (settings.glossaries ?? []).length <= 1) return;

		askConfirm(
			$i18n.t('Delete glossary'),
			$i18n.t('Delete this glossary?'),
			$i18n.t('Delete'),
			async () => {
				loading = true;
				try {
					settings = await deleteGlossary(localStorage.token, settings.active_glossary_id);
					selectedEntries = new Set();
					toast.success($i18n.t('Glossary deleted'));
					await load();
				} catch (error) {
					toast.error(`${error}`);
					loading = false;
				}
			}
		);
	};

	const importEntries = async () => {
		if (!importText.trim()) return;
		importing = true;
		try {
			await importGlossaryEntries(
				localStorage.token,
				importText,
				replaceImport,
				importAsNewGlossary
			);
			importText = '';
			showImport = false;
			replaceImport = false;
			importAsNewGlossary = false;
			toast.success($i18n.t('Glossary imported'));
			await load();
		} catch (error) {
			toast.error(`${error}`);
		}
		importing = false;
	};

	const resetImportModal = () => {
		importText = '';
		showImport = false;
		replaceImport = false;
		importAsNewGlossary = false;
	};

	const closeImportModal = () => {
		if (!importText.trim()) {
			resetImportModal();
			return;
		}

		askConfirm(
			'取消批量添加？',
			'当前输入的内容还没有保存，确定要关闭吗？',
			$i18n.t('Confirm'),
			resetImportModal
		);
	};

	const showBatchImport = () => {
		importAsNewGlossary = false;
		replaceImport = false;
		showImport = true;
	};

	const importGlossaryFile = async (event: Event) => {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		if (!file) return;

		importing = true;
		try {
			const content = await file.text();
			await importGlossaryEntries(localStorage.token, content, false, true);
			toast.success($i18n.t('Glossary imported'));
			await load();
		} catch (error) {
			toast.error(`${error}`);
		} finally {
			importing = false;
			input.value = '';
		}
	};

	const exportActiveGlossary = async () => {
		try {
			const blob = await exportGlossary(localStorage.token);
			const active = (settings.glossaries ?? []).find(
				(item) => item.id === settings.active_glossary_id
			);
			const exportLabel = isSmartMode
				? `${smartSourceLang}-${smartTargetLang}`
				: (active?.name ?? active?.id ?? 'glossary');
			const exportVersion = isSmartMode
				? '1.0.0'
				: (active?.version ?? settings.glossary_version ?? '1.0.0');
			const name = `${safeExportName(exportLabel)}-${exportVersion}.aurapro-glossary.json`;
			const url = URL.createObjectURL(blob);
			const anchor = document.createElement('a');
			anchor.href = url;
			anchor.download = name;
			anchor.click();
			URL.revokeObjectURL(url);
		} catch (error) {
			toast.error(`${error}`);
		}
	};

	onMount(load);
</script>

<div
	class="flex flex-col w-full h-screen max-h-[100dvh] transition-width duration-200 ease-in-out {$showSidebar
		? 'md:max-w-[calc(100%-var(--sidebar-width))]'
		: ''} max-w-full"
>
	<nav class="px-2 pt-1.5 backdrop-blur-xl w-full drag-region">
		<div class="flex items-center">
			{#if $mobile}
				<div class="{$showSidebar ? 'md:hidden' : ''} flex flex-none items-center">
					<Tooltip
						content={$showSidebar ? $i18n.t('Close Sidebar') : $i18n.t('Open Sidebar')}
						interactive={true}
					>
						<button
							id="sidebar-toggle-button"
							class="cursor-pointer flex rounded-lg hover:bg-gray-100 dark:hover:bg-gray-850 transition"
							on:click={() => showSidebar.set(!$showSidebar)}
						>
							<div class="self-center p-1.5">
								<Sidebar />
							</div>
						</button>
					</Tooltip>
				</div>
			{/if}

			<div class="ml-2 py-0.5 self-center flex items-center justify-between w-full">
				<div class="flex items-center gap-2 text-sm font-medium">
					<BookOpen className="size-4" strokeWidth="2" />
					<span>{$i18n.t('Glossary')}</span>
					<span class="text-xs text-gray-500">{entries.length}</span>
				</div>

				<div class="self-center flex items-center gap-1">
					{#if $user !== undefined && $user !== null}
						<UserMenu
							className="w-[240px]"
							role={$user?.role}
							help={true}
							on:show={(e) => {
								if (e.detail === 'archived-chat') showArchivedChats.set(true);
							}}
						>
							<button
								class="select-none flex rounded-xl p-1.5 w-full hover:bg-gray-50 dark:hover:bg-gray-850 transition"
								aria-label="User Menu"
							>
								<img
									src={`${WEBUI_API_BASE_URL}/users/${$user?.id}/profile/image`}
									class="size-6 object-cover rounded-full"
									alt="User profile"
									draggable="false"
								/>
							</button>
						</UserMenu>
					{/if}
				</div>
			</div>
		</div>
	</nav>

	{#if loading}
		<div class="flex-1 flex items-center justify-center">
			<Spinner />
		</div>
	{:else}
		<div class="flex-1 overflow-y-auto px-3 md:px-6 pb-6">
			<div class="max-w-7xl mx-auto py-4 space-y-4">
				<section class="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_320px] gap-4">
					<div class="space-y-3">
						<div class="rounded-lg border border-gray-100 dark:border-gray-800 p-3 space-y-3">
							<div class="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
								<div class="space-y-1">
									<div class="text-xs text-gray-500">{$i18n.t('Glossary mode')}</div>
									<div
										class="inline-flex rounded-lg bg-gray-100 p-1 dark:bg-gray-850"
										role="group"
										aria-label={$i18n.t('Glossary mode')}
									>
										<button
											class="min-w-28 rounded-md px-3 py-1.5 text-sm transition {isSmartMode
												? 'bg-white font-medium text-gray-900 shadow-sm dark:bg-gray-700 dark:text-white'
												: 'text-gray-500 hover:text-gray-800 dark:hover:text-gray-200'}"
											aria-pressed={isSmartMode}
											disabled={savingSettings}
											on:click={() => updateGlossaryMode('smart')}
										>
											{$i18n.t('Smart glossary')}
										</button>
										<button
											class="min-w-28 rounded-md px-3 py-1.5 text-sm transition {!isSmartMode
												? 'bg-white font-medium text-gray-900 shadow-sm dark:bg-gray-700 dark:text-white'
												: 'text-gray-500 hover:text-gray-800 dark:hover:text-gray-200'}"
											aria-pressed={!isSmartMode}
											disabled={savingSettings}
											on:click={() => updateGlossaryMode('fixed')}
										>
											{$i18n.t('Specific glossary')}
										</button>
									</div>
								</div>
								{#if isSmartMode}
									<button
										class="px-3 py-2 rounded-lg text-sm bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800"
										on:click={openCreateGlossaryModal}
									>
										{$i18n.t('Create glossary')}
									</button>
								{/if}
							</div>

							{#if isSmartMode}
								<div class="space-y-3 border-t border-gray-100 pt-3 dark:border-gray-800">
									<div class="grid grid-cols-1 gap-2 md:grid-cols-2">
										<label class="min-w-0 space-y-1">
											<div class="text-xs text-gray-500">{$i18n.t('Language 1')}</div>
											<SearchableSelect
												id="smart-glossary-source-language"
												className="w-full rounded-lg border border-gray-100 bg-gray-50 dark:border-gray-800 dark:bg-gray-900"
												inputClassName="px-3 py-2"
												value={smartSourceLang}
												items={languageSelectItems}
												allowCustom={true}
												placeholder={$i18n.t('Enter or select a language')}
												emptyText={$i18n.t('Enter a custom language')}
												disabled={savingSettings}
												on:change={(event) => updateSmartLanguage('source', event.detail)}
											/>
										</label>
										<label class="min-w-0 space-y-1">
											<div class="text-xs text-gray-500">{$i18n.t('Language 2')}</div>
											<SearchableSelect
												id="smart-glossary-target-language"
												className="w-full rounded-lg border border-gray-100 bg-gray-50 dark:border-gray-800 dark:bg-gray-900"
												inputClassName="px-3 py-2"
												value={smartTargetLang}
												items={languageSelectItems}
												allowCustom={true}
												placeholder={$i18n.t('Enter or select a language')}
												emptyText={$i18n.t('Enter a custom language')}
												disabled={savingSettings}
												on:change={(event) => updateSmartLanguage('target', event.detail)}
											/>
										</label>
									</div>
									<div class="text-xs text-gray-500">
										{$i18n.t(
											'Smart mode uses a direct glossary first, then combines compatible glossaries through a shared language.'
										)}
									</div>
									<div class="space-y-2 border-t border-gray-100 pt-3 dark:border-gray-800">
										{#if glossaryRoutes.length > 0}
											{#each glossaryRoutes as route}
												<div class="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs">
													<span
														class="inline-flex rounded-full px-2 py-0.5 font-medium {route.kind ===
														'direct'
															? 'bg-blue-50 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300'
															: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300'}"
													>
														{route.kind === 'direct'
															? $i18n.t('Direct glossary')
															: $i18n.t('Combined glossary')}
													</span>
													<span class="font-medium text-gray-700 dark:text-gray-200">
														{route.source_lang} ↔ {route.target_lang}
													</span>
													{#if route.kind === 'combined' && route.pivot_lang}
														<span class="text-gray-500">
															{$i18n.t('via {{language}}', { language: route.pivot_lang })}
														</span>
													{/if}
													<span class="text-gray-400">
														{$i18n.t('{{count}} terms', { count: route.coverage })}
													</span>
													{#if route.glossary_names?.length}
														<span
															class="min-w-0 truncate text-gray-400"
															title={route.glossary_names.join(' + ')}
														>
															{route.glossary_names.join(' + ')}
														</span>
													{/if}
												</div>
											{/each}
										{:else}
											<div class="text-xs text-amber-700 dark:text-amber-300">
												{$i18n.t(
													'No compatible glossary was found. Translation will continue without glossary terms.'
												)}
											</div>
										{/if}
									</div>
								</div>
							{:else}
								<div class="flex flex-col md:flex-row gap-2 md:items-end md:justify-between">
									<label class="flex-1 space-y-1">
										<div class="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-gray-500">
											<span>{$i18n.t('Active glossary')}</span>
											<span class="text-gray-400 dark:text-gray-500">
												没有找到所需语言？点击创建词典，设置后即可使用。
											</span>
										</div>
										<div class="flex gap-2">
											<SearchableSelect
												id="active-glossary-selector"
												className="w-full rounded-lg border border-gray-100 bg-gray-50 dark:border-gray-800 dark:bg-gray-900"
												inputClassName="px-3 py-2"
												value={settings.active_glossary_id}
												items={glossarySelectItems}
												placeholder={$i18n.t('Search')}
												emptyText={$i18n.t('No results found')}
												on:change={(event) => switchGlossary(event.detail)}
											/>
											<Tooltip content={$i18n.t('Current glossary settings')}>
												<button
													class="shrink-0 rounded-lg border border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 p-2 text-gray-600 hover:bg-gray-100 dark:hover:bg-gray-850 dark:text-gray-300"
													on:click={() => (showGlossarySettings = !showGlossarySettings)}
													aria-label={$i18n.t('Current glossary settings')}
												>
													<Cog6 className="size-4" strokeWidth="1.8" />
												</button>
											</Tooltip>
										</div>
									</label>
									<button
										class="px-3 py-2 rounded-lg text-sm bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800"
										on:click={openCreateGlossaryModal}
									>
										{$i18n.t('Create glossary')}
									</button>
									<button
										class="px-3 py-2 rounded-lg text-sm bg-red-50 text-red-600 hover:bg-red-100 dark:bg-red-950/30 dark:hover:bg-red-950/50 disabled:opacity-40 disabled:cursor-not-allowed"
										disabled={activeGlossary?.official || (settings.glossaries ?? []).length <= 1}
										on:click={removeActiveGlossary}
									>
										{$i18n.t('Delete glossary')}
									</button>
								</div>

								<div class="text-xs text-gray-500 space-y-1">
									<div class="flex flex-wrap items-center gap-x-2 gap-y-1">
										{#if activeGlossaryPair.trim() !== '->'}
											<span class="font-medium text-gray-700 dark:text-gray-200">
												{activeGlossaryPair}
											</span>
										{/if}
										{#if activeGlossary?.official}
											<span
												class="inline-flex rounded-full bg-blue-50 px-2 py-0.5 text-[11px] font-medium text-blue-600 dark:bg-blue-950/40 dark:text-blue-300"
											>
												{$i18n.t('Official')}
											</span>
										{/if}
										<span class="font-mono">v{activeGlossaryVersion}</span>
										{#if activeGlossaryFile}
											<span class="font-mono">{activeGlossaryFile}</span>
										{/if}
									</div>
									{#if activeGlossary?.official}
										<div>
											{$i18n.t(
												'Official glossaries stay updateable. When you edit one, AuraPro creates an edited user copy.'
											)}
										</div>
									{/if}
								</div>

								{#if showGlossarySettings}
									<div
										class="rounded-lg border border-gray-100 bg-gray-50/70 p-3 dark:border-gray-800 dark:bg-gray-900/60 space-y-3"
									>
										<div class="flex items-center justify-between gap-2">
											<div>
												<div class="text-sm font-medium">
													{$i18n.t('Current glossary settings')}
												</div>
												<div class="text-xs text-gray-500">
													{$i18n.t('These settings follow the selected glossary.')}
												</div>
											</div>
											<button
												class="rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-850 dark:hover:text-gray-200"
												on:click={() => (showGlossarySettings = false)}
												aria-label={$i18n.t('Close')}
											>
												<XMark className="size-4" strokeWidth="2" />
											</button>
										</div>
										<div class="grid grid-cols-1 md:grid-cols-2 gap-2">
											<label class="block space-y-1 md:col-span-2">
												<div class="text-xs text-gray-500">{$i18n.t('Glossary JSON path')}</div>
												<input
													class="w-full rounded-lg bg-white dark:bg-gray-950 border border-gray-100 dark:border-gray-800 px-3 py-2 text-sm outline-none"
													bind:value={settings.glossary_path}
												/>
											</label>
											<label class="block space-y-1">
												<div class="text-xs text-gray-500">{$i18n.t('Glossary version')}</div>
												<input
													class="w-full rounded-lg bg-white dark:bg-gray-950 border border-gray-100 dark:border-gray-800 px-3 py-2 text-sm outline-none"
													bind:value={settings.glossary_version}
												/>
											</label>
											<label class="block space-y-1">
												<div class="text-xs text-gray-500">{$i18n.t('Source language')}</div>
												<input
													class="w-full rounded-lg bg-white dark:bg-gray-950 border border-gray-100 dark:border-gray-800 px-3 py-2 text-sm outline-none"
													bind:value={settings.source_lang}
												/>
											</label>
											<label class="block space-y-1">
												<div class="text-xs text-gray-500">{$i18n.t('Target language')}</div>
												<input
													class="w-full rounded-lg bg-white dark:bg-gray-950 border border-gray-100 dark:border-gray-800 px-3 py-2 text-sm outline-none"
													bind:value={settings.target_lang}
												/>
											</label>
											<div class="flex items-end">
												<button
													class="w-full px-3 py-2 rounded-lg text-sm bg-black text-white dark:bg-white dark:text-black disabled:opacity-50"
													disabled={savingSettings}
													on:click={saveSettings}
												>
													{savingSettings
														? $i18n.t('Saving...')
														: $i18n.t('Save glossary settings')}
												</button>
											</div>
										</div>
									</div>
								{/if}
							{/if}
						</div>

						<div class="flex flex-col md:flex-row gap-2 md:items-center md:justify-between">
							<div class="relative flex-1 max-w-xl">
								<div class="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400">
									<Search className="size-4" strokeWidth="2" />
								</div>
								<input
									class="w-full pl-9 pr-3 py-2 rounded-lg bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 text-sm outline-none"
									bind:value={query}
									placeholder={$i18n.t('Search glossary')}
								/>
							</div>

							<div class="flex gap-2">
								{#if selectedEntries.size > 0}
									<button
										class="px-3 py-2 rounded-lg text-sm bg-red-50 text-red-600 hover:bg-red-100 dark:bg-red-950/30 dark:hover:bg-red-950/50"
										on:click={removeSelectedEntries}
									>
										{$i18n.t('Delete selected')} ({selectedEntries.size})
									</button>
								{/if}
								<button
									class="px-3 py-2 rounded-lg text-sm bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800"
									on:click={exportActiveGlossary}
								>
									{$i18n.t('Export')}
								</button>
								<button
									class="px-3 py-2 rounded-lg text-sm bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800 disabled:opacity-60 flex items-center gap-2"
									on:click={() => glossaryFileInput?.click()}
									disabled={importing}
								>
									<DocumentArrowUp className="size-4" strokeWidth="2" />
									{$i18n.t('Import glossary')}
								</button>
								<div class="relative group">
									<button
										class="px-3 py-2 rounded-lg text-sm bg-black text-white dark:bg-white dark:text-black flex items-center gap-2"
										on:click={addEntry}
									>
										<Plus className="size-4" strokeWidth="2" />
										{$i18n.t('New term')}
									</button>
									<div
										class="hidden group-hover:block absolute right-0 top-full z-20 min-w-44 pt-1"
									>
										<div
											class="rounded-lg border border-gray-100 bg-white p-1 text-sm shadow-lg dark:border-gray-800 dark:bg-gray-900"
										>
											<button
												class="w-full rounded-md px-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-850"
												on:click={showBatchImport}
											>
												批量添加
											</button>
										</div>
									</div>
								</div>
								<input
									class="hidden"
									type="file"
									accept=".json,.aurapro-glossary.json,.csv,.tsv,.txt,application/json,text/csv,text/tab-separated-values,text/plain"
									bind:this={glossaryFileInput}
									on:change={importGlossaryFile}
								/>
							</div>
						</div>

						<div class="overflow-x-auto rounded-lg border border-gray-100 dark:border-gray-800">
							<div
								class="grid grid-cols-[44px_minmax(140px,1fr)_minmax(140px,1fr)_156px] bg-gray-50 dark:bg-gray-900 text-xs font-medium text-gray-500"
							>
								<div class="px-3 py-2 flex items-center justify-center">
									<input
										type="checkbox"
										checked={allFilteredSelected}
										disabled={selectableFilteredEntries.length === 0}
										on:change={toggleFilteredSelectionFromEvent}
									/>
								</div>
								<div class="px-3 py-2">{$i18n.t('Source')}</div>
								<div class="px-3 py-2 border-l border-gray-100 dark:border-gray-800">
									{$i18n.t('Translation')}
								</div>
								<div class="px-3 py-2 border-l border-gray-100 dark:border-gray-800 text-right">
									{$i18n.t('Actions')}
								</div>
							</div>

							{#if filteredEntries.length === 0}
								<div class="py-16 text-center text-sm text-gray-500">
									{$i18n.t('No glossary entries found')}
								</div>
							{/if}

							{#each pagedEntries as entry (entry.source || entry)}
								<div
									class="grid min-h-14 grid-cols-[44px_minmax(140px,1fr)_minmax(140px,1fr)_156px] border-t border-gray-100 dark:border-gray-800"
								>
									<div class="px-3 py-2 flex items-center justify-center">
										<input
											type="checkbox"
											disabled={!entry.source || entry.origin !== 'user'}
											checked={entry.source ? selectedEntries.has(entry.source) : false}
											on:change={(event) => toggleEntrySelectionFromEvent(entry.source, event)}
										/>
									</div>
									<div class="min-w-0 px-3 py-1.5">
										<input
											class="block w-full bg-transparent py-0.5 text-sm outline-none read-only:text-gray-600 dark:read-only:text-gray-300"
											bind:value={entry.draftSource}
											readonly={entry.origin !== 'user' && Boolean(entry.source)}
											on:input={() => (entry.dirty = true)}
											on:blur={() =>
												entry.origin === 'user' &&
												entry.dirty &&
												entry.draftSource.trim() &&
												entry.draftTarget.trim() &&
												saveEntry(entry)}
										/>
										<span
											class="inline-flex rounded-full px-1.5 py-0.5 text-[10px] font-medium {entry.origin ===
											'user'
												? 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300'
												: entry.origin === 'direct'
													? 'bg-blue-50 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300'
													: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300'}"
										>
											{entry.origin === 'user'
												? $i18n.t('Personal term')
												: entry.origin === 'direct'
													? $i18n.t('Direct glossary')
													: $i18n.t('Combined glossary')}
										</span>
									</div>
									<input
										class="min-w-0 border-l border-gray-100 bg-transparent px-3 py-2 text-sm outline-none read-only:text-gray-600 dark:border-gray-800 dark:read-only:text-gray-300"
										bind:value={entry.draftTarget}
										readonly={!isEntryEditable(entry)}
										on:input={() => (entry.dirty = true)}
										on:blur={() =>
											entry.origin === 'user' &&
											entry.dirty &&
											entry.draftSource.trim() &&
											entry.draftTarget.trim() &&
											saveEntry(entry)}
									/>
									<div
										class="px-2 py-1.5 flex items-center justify-end gap-1 border-l border-gray-100 dark:border-gray-800"
									>
										{#if entry.origin !== 'user' && !entry.editingOverride}
											<button
												class="rounded-md bg-gray-100 px-2 py-1.5 text-xs text-gray-700 hover:bg-gray-200 dark:bg-gray-850 dark:text-gray-200 dark:hover:bg-gray-800"
												on:click={() => beginPersonalCorrection(entry)}
											>
												{$i18n.t('Personal correction')}
											</button>
										{:else}
											<button
												class="px-2 py-1 rounded-md text-xs {entry.dirty
													? 'bg-black text-white dark:bg-white dark:text-black'
													: 'text-gray-400'}"
												disabled={!entry.dirty}
												on:click={() => saveEntry(entry)}
											>
												{$i18n.t('Save')}
											</button>
											{#if entry.origin !== 'user'}
												<button
													class="rounded-md px-2 py-1 text-xs text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-850"
													on:click={() => cancelPersonalCorrection(entry)}
												>
													{$i18n.t('Cancel')}
												</button>
											{:else}
												<button
													class="p-1.5 rounded-md text-gray-500 hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-950/30"
													on:click={() => removeEntry(entry)}
													aria-label={$i18n.t('Delete')}
												>
													<XMark className="size-4" strokeWidth="2" />
												</button>
											{/if}
										{/if}
									</div>
								</div>
							{/each}
						</div>
					</div>

					<aside class="space-y-3">
						<div class="rounded-lg border border-gray-100 dark:border-gray-800 p-3 space-y-3">
							<div>
								<div class="text-sm font-medium">{$i18n.t('Global translation settings')}</div>
								<div class="mt-0.5 text-xs text-gray-500">
									{$i18n.t(
										'Applies to translation, simultaneous interpretation, and learning modes.'
									)}
								</div>
							</div>
							<div class="grid grid-cols-1 gap-2">
								<label class="block space-y-1">
									<div class="text-xs text-gray-500">{$i18n.t('Terms')}</div>
									<input
										type="number"
										class="w-full rounded-lg bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 px-3 py-2 text-sm outline-none"
										bind:value={settings.max_terms_injected}
									/>
								</label>
								<label class="block space-y-1">
									<div class="text-xs text-gray-500">{$i18n.t('Turns')}</div>
									<input
										type="number"
										class="w-full rounded-lg bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 px-3 py-2 text-sm outline-none"
										bind:value={settings.max_turns}
									/>
								</label>
								<label class="block space-y-1">
									<div class="text-xs text-gray-500">{$i18n.t('Tokens')}</div>
									<input
										type="number"
										class="w-full rounded-lg bg-gray-50 dark:bg-gray-900 border border-gray-100 dark:border-gray-800 px-3 py-2 text-sm outline-none"
										bind:value={settings.token_limit}
									/>
								</label>
							</div>
							<button
								class="w-full px-3 py-2 rounded-lg text-sm bg-black text-white dark:bg-white dark:text-black disabled:opacity-50"
								disabled={savingSettings}
								on:click={saveSettings}
							>
								{savingSettings ? $i18n.t('Saving...') : $i18n.t('Save global settings')}
							</button>
						</div>

						<div
							class="rounded-lg border border-gray-100 dark:border-gray-800 p-3 text-xs text-gray-500 space-y-2"
						>
							<div class="font-medium text-gray-700 dark:text-gray-200">
								{$i18n.t('Import formats')}
							</div>
							<div>JSON: {`{"你好":"hello"}`}</div>
							<div>CSV: 你好,hello</div>
							<div>Lines: 你好 -&gt; hello</div>
						</div>
					</aside>
				</section>

				<!-- ↓ 插在這裡 -->
				{#if totalPages > 1}
					<div
						class="flex items-center justify-between px-3 py-2 border-t border-gray-100 dark:border-gray-800 text-sm text-gray-500"
					>
						<span
							>{(currentPage - 1) * pageSize + 1}–{Math.min(
								currentPage * pageSize,
								filteredEntries.length
							)} / {filteredEntries.length}</span
						>
						<div class="flex gap-1">
							<button
								class="px-2 py-1 rounded-md hover:bg-gray-100 dark:hover:bg-gray-850 disabled:opacity-30"
								disabled={currentPage === 1}
								on:click={() => currentPage--}>‹</button
							>
							<span class="px-2 py-1">{currentPage} / {totalPages}</span>
							<button
								class="px-2 py-1 rounded-md hover:bg-gray-100 dark:hover:bg-gray-850 disabled:opacity-30"
								disabled={currentPage === totalPages}
								on:click={() => currentPage++}>›</button
							>
						</div>
					</div>
				{/if}
			</div>
		</div>
	{/if}

	{#if showCreateGlossary}
		<div class="fixed inset-0 z-[1000] flex items-center justify-center bg-black/30 px-4">
			<div
				class="w-full max-w-lg rounded-lg border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 p-4 shadow-xl"
			>
				<div class="flex items-start justify-between gap-3">
					<div>
						<div class="text-base font-medium text-gray-900 dark:text-gray-100">
							{$i18n.t('Create glossary')}
						</div>
						<div class="mt-1 text-sm text-gray-500">
							{$i18n.t('Leave the name empty to use source-target naming automatically.')}
						</div>
					</div>
					<button
						class="rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-850 dark:hover:text-gray-200"
						on:click={closeCreateGlossaryModal}
						aria-label={$i18n.t('Close')}
					>
						<XMark className="size-4" strokeWidth="2" />
					</button>
				</div>

				<div class="mt-4 space-y-3">
					<label class="block space-y-1">
						<div class="text-xs text-gray-500">{$i18n.t('Glossary name')}</div>
						<input
							class="w-full rounded-lg bg-gray-50 dark:bg-gray-950 border border-gray-100 dark:border-gray-800 px-3 py-2 text-sm outline-none"
							bind:value={newGlossaryName}
							placeholder={$i18n.t('Optional')}
							on:keydown={(e) => {
								if (e.key === 'Enter') createNewGlossary();
							}}
						/>
					</label>
					<div class="grid grid-cols-1 md:grid-cols-2 gap-3">
						<label class="block space-y-1">
							<div class="text-xs text-gray-500">{$i18n.t('Source language')}</div>
							<input
								class="w-full rounded-lg bg-gray-50 dark:bg-gray-950 border border-gray-100 dark:border-gray-800 px-3 py-2 text-sm outline-none"
								bind:value={newGlossarySourceLang}
								placeholder={settings.source_lang ?? $i18n.t('Source language')}
								on:keydown={(e) => {
									if (e.key === 'Enter') createNewGlossary();
								}}
							/>
						</label>
						<label class="block space-y-1">
							<div class="text-xs text-gray-500">{$i18n.t('Target language')}</div>
							<input
								class="w-full rounded-lg bg-gray-50 dark:bg-gray-950 border border-gray-100 dark:border-gray-800 px-3 py-2 text-sm outline-none"
								bind:value={newGlossaryTargetLang}
								placeholder={settings.target_lang ??
									settings.glossary_lang ??
									$i18n.t('Target language')}
								on:keydown={(e) => {
									if (e.key === 'Enter') createNewGlossary();
								}}
							/>
						</label>
					</div>
				</div>

				<div class="mt-5 flex justify-end gap-2">
					<button
						class="px-3 py-2 rounded-lg text-sm bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800"
						on:click={closeCreateGlossaryModal}
					>
						{$i18n.t('Cancel')}
					</button>
					<button
						class="px-3 py-2 rounded-lg text-sm bg-black text-white dark:bg-white dark:text-black disabled:opacity-50"
						disabled={!(
							(newGlossarySourceLang.trim() || settings.source_lang) &&
							(newGlossaryTargetLang.trim() || settings.target_lang || settings.glossary_lang)
						)}
						on:click={createNewGlossary}
					>
						{$i18n.t('Create')}
					</button>
				</div>
			</div>
		</div>
	{/if}

	{#if showImport}
		<div class="fixed inset-0 z-[1000] flex items-center justify-center bg-black/30 px-4">
			<div
				class="w-full max-w-2xl rounded-lg border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 p-4 shadow-xl"
			>
				<!-- 頂部標題列 -->
				<div class="flex items-start justify-between gap-3">
					<div>
						<!-- 標題與幫助圖標容器 (Flex 布局) -->
						<div class="flex items-center gap-2">
							<span class="text-base font-medium text-gray-900 dark:text-gray-100">批量添加</span>

							<!-- 幫助圖標與懸浮提示框 -->
							<div class="relative group flex items-center">
								<button
									type="button"
									class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
								>
									<svg
										xmlns="http://www.w3.org/2000/svg"
										fill="none"
										viewBox="0 0 24 24"
										stroke-width="2"
										stroke="currentColor"
										class="size-4"
									>
										<path
											stroke-linecap="round"
											stroke-linejoin="round"
											d="M9.879 7.519c1.171-1.025 3.071-1.025 4.242 0 1.172 1.025 1.172 2.687 0 3.712-.203.179-.43.326-.67.442-.745.361-1.45.999-1.45 1.827v.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 5.25h.008v.008H12v-.008Z"
										/>
									</svg>
								</button>

								<!-- 懸浮提示框 (數據渲染) -->
								<div
									class="absolute top-full left-1/2 z-50 mt-2 hidden w-[32rem] -translate-x-1/2 rounded-lg border border-gray-200 bg-white shadow-2xl group-hover:block dark:border-gray-700 dark:bg-gray-800"
								>
									<div class="max-h-[400px] overflow-y-auto p-4 custom-scrollbar">
										<div
											class="mb-3 text-sm font-semibold text-gray-900 dark:text-gray-100 border-b border-gray-100 dark:border-gray-700 pb-2"
										>
											词典格式说明
										</div>

										<div class="space-y-4">
											{#each importHelpSections as section}
												<div>
													<h4
														class="text-xs font-bold {section.type === 'error'
															? 'text-red-600 dark:text-red-400'
															: 'text-gray-800 dark:text-gray-200'}"
													>
														{section.title}
													</h4>
													{#if section.subtitle}
														<p class="text-[11px] text-gray-500 mb-1.5 mt-0.5">
															{section.subtitle}
														</p>
													{/if}

													{#if section.type === 'default'}
														<div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
															{#each section.items as item}
																<pre
																	class="whitespace-pre-wrap rounded bg-gray-50 p-2 text-[11px] font-mono text-gray-600 dark:bg-gray-900 dark:text-gray-300">{item.code}</pre>
															{/each}
														</div>
													{:else}
														<div class="space-y-2 pt-2">
															{#each section.items as item}
																<div
																	class="rounded-md bg-red-50/50 p-2 text-[11px] dark:bg-red-950/20"
																>
																	<div class="text-red-600 dark:text-red-400 font-mono">
																		❌ {item.bad}
																	</div>
																	<div class="text-gray-500 pl-4 my-0.5">→ {item.reason}</div>
																	<div class="text-green-600 dark:text-green-500 font-mono mt-1">
																		✅ {item.good}
																	</div>
																</div>
															{/each}
														</div>
													{/if}
												</div>
											{/each}
										</div>
									</div>
									<!-- 三角形指示器 -->
									<div
										class="absolute -top-2 left-1/2 -ml-2 h-0 w-0 border-x-8 border-b-8 border-x-transparent border-b-white dark:border-b-gray-800"
									></div>
								</div>
							</div>
						</div>

						<!-- 副標題 -->
						<div class="mt-1 text-sm text-gray-500">
							每行一条，支持 JSON、CSV/TSV、或“中文 -> 外文”的格式。
						</div>
					</div>

					<!-- 關閉按鈕 -->
					<button
						class="rounded-lg p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-850 dark:hover:text-gray-200"
						on:click={closeImportModal}
						aria-label="Close"
					>
						<XMark className="size-4" strokeWidth="2" />
					</button>
				</div>

				<!-- 輸入區域 -->
				<textarea
					class="mt-4 w-full min-h-64 rounded-lg bg-gray-50 dark:bg-gray-950 border border-gray-100 dark:border-gray-800 p-3 text-sm outline-none font-mono"
					bind:value={importText}
					placeholder={`{"你好": "hello"}\n你好 -> hello\n你好, hello`}
				/>

				<!-- 底部動作欄 -->
				<div class="mt-3 flex flex-col md:flex-row gap-2 md:items-center md:justify-between">
					<label class="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
						<input type="checkbox" bind:checked={replaceImport} />
						{$i18n.t('Replace existing glossary')}
					</label>
					<div class="flex justify-end gap-2">
						<button
							class="px-3 py-2 rounded-lg text-sm bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800"
							on:click={closeImportModal}
						>
							{$i18n.t('Cancel')}
						</button>
						<button
							class="px-3 py-2 rounded-lg text-sm bg-black text-white dark:bg-white dark:text-black disabled:opacity-50"
							disabled={importing || !importText.trim()}
							on:click={importEntries}
						>
							{importing ? $i18n.t('Importing...') : $i18n.t('Confirm')}
						</button>
					</div>
				</div>
			</div>
		</div>
	{/if}

	{#if confirmDialog.show}
		<div class="fixed inset-0 z-[1000] flex items-center justify-center bg-black/30 px-4">
			<div
				class="w-full max-w-sm rounded-lg border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 p-4 shadow-xl"
			>
				<div class="text-base font-medium text-gray-900 dark:text-gray-100">
					{confirmDialog.title}
				</div>
				<div class="mt-2 text-sm text-gray-600 dark:text-gray-300">
					{confirmDialog.message}
				</div>
				<div class="mt-4 flex justify-end gap-2">
					<button
						class="px-3 py-2 rounded-lg text-sm bg-gray-100 dark:bg-gray-850 hover:bg-gray-200 dark:hover:bg-gray-800"
						on:click={() => (confirmDialog = { ...confirmDialog, show: false, onConfirm: null })}
					>
						{$i18n.t('Cancel')}
					</button>
					<button
						class="px-3 py-2 rounded-lg text-sm bg-red-600 text-white hover:bg-red-700"
						on:click={runConfirmedAction}
					>
						{confirmDialog.confirmLabel}
					</button>
				</div>
			</div>
		</div>
	{/if}
</div>
