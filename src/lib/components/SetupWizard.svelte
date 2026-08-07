<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import { toast } from 'svelte-sonner';
	import type { i18n as i18nType } from 'i18next';

	import { getBackendConfig } from '$lib/apis';
	import { getAdminConfig, updateAdminConfig } from '$lib/apis/auths';
	import { getAudioConfig, updateAudioConfig } from '$lib/apis/audio';
	import { getChatConfig, updateChatConfig } from '$lib/apis/chats';
	import {
		activateGlossary,
		createGlossary,
		getGlossarySettings,
		updateGlossarySettings
	} from '$lib/apis/glossary';
	import { getOllamaConfig, updateOllamaConfig } from '$lib/apis/ollama';
	import { getOpenAIConfig, updateOpenAIConfig } from '$lib/apis/openai';
	import { updateUserSettings } from '$lib/apis/users';
	import { AURAPRO_SETUP_WIZARD_VERSION } from '$lib/constants';
	import { config, settings, user } from '$lib/stores';
	import {
		applyExtensionMode,
		normalizeExtensionMode,
		type ExtensionMode
	} from '$lib/utils/extension-modes';

	import Modal from '$lib/components/common/Modal.svelte';
	import SearchableSelect from '$lib/components/common/SearchableSelect.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	const i18n: Writable<i18nType> = getContext('i18n');

	export let show = false;

	type Purpose = 'translation' | 'manuscript_translation' | 'other';

	interface GlossaryOption {
		id: string;
		name?: string;
		source_lang?: string;
		target_lang?: string;
		glossary_lang?: string;
		version?: string;
		path?: string;
		official?: boolean;
	}

	let loading = false;
	let saving = false;
	let initializedForOpen = false;

	let purpose: Purpose = 'other';
	let defaultExtensionMode: ExtensionMode = '';
	let contextSize = 16384;
	let mtpEnabled = false;
	let multimodalEnabled = true;
	let glossarySettings: {
		active_glossary_id?: string;
		glossaries?: GlossaryOption[];
		source_lang?: string;
		target_lang?: string;
		glossary_lang?: string;
		token_limit?: number;
		mtp_enabled?: boolean;
		multimodal_enabled?: boolean;
	} = {};
	let selectedGlossaryId = '';
	let customSourceLanguage = '';
	let customTargetLanguage = '';
	let customGlossaryName = '';
	let speechRecognitionMode = '';
	let adminAudioConfig: any = null;
	let adminGeneralConfig: any = null;
	let adminChatConfig: any = null;
	let memoryBackgroundReviewEnabled = false;
	let contextCompactionEnabled = true;

	$: if (show && !initializedForOpen) {
		initializedForOpen = true;
		void loadWizard();
	}
	$: if (!show) {
		initializedForOpen = false;
	}

	const extensionModeLabel = (mode: ExtensionMode): string => {
		const labels: Record<ExtensionMode, string> = {
			'': $i18n.t('None'),
			translation: $i18n.t('Translation Mode'),
			manuscript_translation: $i18n.t('Manuscript Translation Mode'),
			interpretation: $i18n.t('Simultaneous Interpretation'),
			learning: $i18n.t('Learning Mode'),
			rag_translation: $i18n.t('RAG Translation Mode')
		};
		return labels[mode];
	};

	const glossaryLabel = (glossary: GlossaryOption): string => {
		const source = glossary.source_lang ?? glossarySettings.source_lang ?? '';
		const target =
			glossary.target_lang ??
			glossary.glossary_lang ??
			glossarySettings.target_lang ??
			glossarySettings.glossary_lang ??
			'';
		const pair = source && target ? `${source} → ${target}` : glossary.name || glossary.id;
		return `${pair}${glossary.name ? ` · ${glossary.name}` : ''}${
			glossary.version ? ` v${glossary.version}` : ''
		}`;
	};

	$: glossarySelectItems = [
		...(glossarySettings.glossaries ?? []).map((glossary) => ({
			value: glossary.id,
			label: glossaryLabel(glossary),
			searchTerms: [
				glossary.name,
				glossary.source_lang,
				glossary.target_lang,
				glossary.glossary_lang,
				glossary.version,
				glossary.path
			]
		})),
		{
			value: '__other__',
			label: $i18n.t('Other languages'),
			searchTerms: ['other', '其他语言']
		}
	];

	const selectPurpose = (nextPurpose: Purpose) => {
		purpose = nextPurpose;

		if ($user?.role !== 'admin') return;

		memoryBackgroundReviewEnabled = false;
		contextCompactionEnabled = nextPurpose === 'other';
	};

	const isLocalLlamaConnectionUrl = (url: unknown): boolean =>
		typeof url === 'string' && url.includes('127.0.0.1:18881');

	const withLocalLlamaConnectionTypes = (
		urls: string[] = [],
		apiConfigs: Record<string, any> = {}
	): { configs: Record<string, any>; changed: boolean } => {
		let changed = false;
		const configs = { ...(apiConfigs ?? {}) };

		urls.forEach((url, idx) => {
			if (!isLocalLlamaConnectionUrl(url)) return;

			const key = `${idx}`;
			const existingConfig = configs[key] ?? configs[url] ?? {};
			if (existingConfig?.connection_type === 'local' && existingConfig?.provider === 'llama.cpp')
				return;

			configs[key] = {
				...existingConfig,
				connection_type: 'local',
				provider: 'llama.cpp'
			};
			changed = true;
		});

		return { configs, changed };
	};

	const fixLocalLlamaConnectionTypes = async () => {
		if ($user?.role !== 'admin') return;

		try {
			const [openAIConfig, ollamaConfig] = await Promise.all([
				getOpenAIConfig(localStorage.token),
				getOllamaConfig(localStorage.token)
			]);

			const openAIUpdate = withLocalLlamaConnectionTypes(
				openAIConfig?.OPENAI_API_BASE_URLS ?? [],
				openAIConfig?.OPENAI_API_CONFIGS ?? {}
			);
			const ollamaUpdate = withLocalLlamaConnectionTypes(
				ollamaConfig?.OLLAMA_BASE_URLS ?? [],
				ollamaConfig?.OLLAMA_API_CONFIGS ?? {}
			);

			const updates = [];
			if (openAIUpdate.changed) {
				updates.push(
					updateOpenAIConfig(localStorage.token, {
						...openAIConfig,
						OPENAI_API_CONFIGS: openAIUpdate.configs
					})
				);
			}
			if (ollamaUpdate.changed) {
				updates.push(
					updateOllamaConfig(localStorage.token, {
						...ollamaConfig,
						OLLAMA_API_CONFIGS: ollamaUpdate.configs
					})
				);
			}

			if (updates.length > 0) {
				await Promise.all(updates);
				config.set(await getBackendConfig());
			}
		} catch (error) {
			console.error('Failed to normalize local llama.cpp connection type:', error);
		}
	};

	const loadWizard = async () => {
		loading = true;
		purpose = $settings?.setupPurpose ?? 'other';
		defaultExtensionMode = normalizeExtensionMode($settings?.defaultExtensionMode);
		const storedContextSize = Number($settings?.params?.num_ctx ?? 16384);
		contextSize = Number.isFinite(storedContextSize) ? storedContextSize : 16384;
		customSourceLanguage = '';
		customTargetLanguage = '';
		customGlossaryName = '';

		await fixLocalLlamaConnectionTypes();

		try {
			glossarySettings = (await getGlossarySettings(localStorage.token)) ?? {};
			selectedGlossaryId =
				glossarySettings.active_glossary_id ?? glossarySettings.glossaries?.[0]?.id ?? '__other__';
			const glossaryContextSize = Number(glossarySettings.token_limit ?? storedContextSize);
			contextSize =
				Number.isFinite(glossaryContextSize) && glossaryContextSize > 0
					? glossaryContextSize
					: 16384;
			mtpEnabled = glossarySettings.mtp_enabled ?? false;
			multimodalEnabled = glossarySettings.multimodal_enabled ?? true;
		} catch (error) {
			console.error('Failed to load glossary settings for setup wizard:', error);
			glossarySettings = {};
			selectedGlossaryId = '__other__';
			mtpEnabled = false;
			multimodalEnabled = true;
		}

		if ($user?.role === 'admin') {
			try {
				[adminAudioConfig, adminGeneralConfig, adminChatConfig] = await Promise.all([
					getAudioConfig(localStorage.token),
					getAdminConfig(localStorage.token),
					getChatConfig(localStorage.token)
				]);
				speechRecognitionMode =
					adminAudioConfig?.stt?.ENGINE === 'multimodal' ? 'multimodal' : 'sherpa';
				memoryBackgroundReviewEnabled =
					adminGeneralConfig?.ENABLE_MEMORY_BACKGROUND_REVIEW ?? false;
				contextCompactionEnabled =
					adminChatConfig?.ENABLE_CONTEXT_COMPACTION ?? purpose === 'other';
			} catch (error) {
				console.error('Failed to load administrator setup settings:', error);
				adminAudioConfig = null;
				adminGeneralConfig = null;
				adminChatConfig = null;
				speechRecognitionMode = 'sherpa';
				memoryBackgroundReviewEnabled = false;
				contextCompactionEnabled = purpose === 'other';
			}
		} else {
			speechRecognitionMode = $settings?.audio?.stt?.engine === 'multimodal' ? 'multimodal' : '';
		}

		loading = false;
	};

	const saveAdminSpeechMode = async () => {
		if ($user?.role !== 'admin' || purpose !== 'translation') return;
		if (!adminAudioConfig) {
			adminAudioConfig = await getAudioConfig(localStorage.token);
		}

		const nextAudioConfig = {
			...adminAudioConfig,
			stt: {
				...adminAudioConfig.stt,
				ENGINE: speechRecognitionMode,
				OPENAI_API_BASE_URL:
					speechRecognitionMode === 'sherpa'
						? adminAudioConfig.stt?.OPENAI_API_BASE_URL || 'http://127.0.0.1:39384/v1'
						: (adminAudioConfig.stt?.OPENAI_API_BASE_URL ?? ''),
				OPENAI_API_KEY:
					speechRecognitionMode === 'sherpa'
						? adminAudioConfig.stt?.OPENAI_API_KEY || 'aurapro-local'
						: (adminAudioConfig.stt?.OPENAI_API_KEY ?? ''),
				MODEL:
					speechRecognitionMode === 'sherpa'
						? adminAudioConfig.stt?.MODEL || 'sherpa-asr'
						: (adminAudioConfig.stt?.MODEL ?? '')
			}
		};

		adminAudioConfig = await updateAudioConfig(localStorage.token, nextAudioConfig);
		config.set(await getBackendConfig());
	};

	const saveAdminFeatureToggles = async () => {
		if ($user?.role !== 'admin') return;

		if (!adminGeneralConfig) {
			adminGeneralConfig = await getAdminConfig(localStorage.token);
		}
		if (!adminChatConfig) {
			adminChatConfig = await getChatConfig(localStorage.token);
		}

		[adminGeneralConfig, adminChatConfig] = await Promise.all([
			updateAdminConfig(localStorage.token, {
				...adminGeneralConfig,
				ENABLE_MEMORY_BACKGROUND_REVIEW: memoryBackgroundReviewEnabled
			}),
			updateChatConfig(localStorage.token, {
				...adminChatConfig,
				ENABLE_CONTEXT_COMPACTION: contextCompactionEnabled
			})
		]);
		config.set(await getBackendConfig());
	};

	const saveGlossarySelection = async () => {
		if (!['translation', 'manuscript_translation'].includes(purpose)) return;

		if (selectedGlossaryId === '__other__') {
			const sourceLanguage = customSourceLanguage.trim();
			const targetLanguage = customTargetLanguage.trim();
			if (!sourceLanguage || !targetLanguage) {
				throw new Error($i18n.t('Source language and target language are required.'));
			}
			glossarySettings = await createGlossary(
				localStorage.token,
				customGlossaryName.trim(),
				sourceLanguage,
				targetLanguage,
				targetLanguage
			);
			selectedGlossaryId = glossarySettings.active_glossary_id ?? selectedGlossaryId;
			return;
		}

		if (selectedGlossaryId && selectedGlossaryId !== glossarySettings.active_glossary_id) {
			glossarySettings = await activateGlossary(localStorage.token, selectedGlossaryId);
		}
	};

	const normalizedContextSize = (): number => Math.max(1, Math.trunc(Number(contextSize) || 16384));

	const saveLlamaRuntimeSettings = async () => {
		glossarySettings = await updateGlossarySettings(localStorage.token, {
			token_limit: normalizedContextSize(),
			mtp_enabled: mtpEnabled,
			multimodal_enabled: multimodalEnabled
		});
	};

	const buildNextUserSettings = () => {
		const nextSettings: any = {
			...$settings,
			setupWizardVersion: AURAPRO_SETUP_WIZARD_VERSION,
			setupPurpose: purpose,
			defaultExtensionMode,
			params: {
				...($settings?.params ?? {}),
				num_ctx: normalizedContextSize()
			}
		};

		if ($user?.role !== 'admin' && purpose === 'translation') {
			nextSettings.audio = {
				...($settings?.audio ?? {}),
				stt: {
					...($settings?.audio?.stt ?? {}),
					engine: speechRecognitionMode || undefined
				}
			};
		}

		return nextSettings;
	};

	const saveWizard = async () => {
		if (saving) return;
		saving = true;
		try {
			await saveGlossarySelection();
			await saveLlamaRuntimeSettings();
			await saveAdminSpeechMode();
			await saveAdminFeatureToggles();

			const nextSettings = buildNextUserSettings();
			await updateUserSettings(localStorage.token, { ui: nextSettings });
			settings.set(nextSettings);
			applyExtensionMode(defaultExtensionMode);
			toast.success($i18n.t('Setup completed'));
			show = false;
		} catch (error) {
			console.error('Failed to save setup wizard:', error);
			toast.error(error instanceof Error ? error.message : `${error}`);
		} finally {
			saving = false;
		}
	};

	const skipWizard = async () => {
		if (saving) return;
		saving = true;
		try {
			const nextSettings = {
				...$settings,
				setupWizardVersion: AURAPRO_SETUP_WIZARD_VERSION
			};
			await updateUserSettings(localStorage.token, { ui: nextSettings });
			settings.set(nextSettings);
			show = false;
		} catch (error) {
			toast.error(error instanceof Error ? error.message : `${error}`);
		} finally {
			saving = false;
		}
	};
</script>

<Modal
	bind:show
	size="lg"
	containerClassName="p-3"
	className="bg-white dark:bg-gray-900 rounded-2xl overflow-hidden"
>
	<form
		class="flex max-h-[min(760px,calc(100dvh-24px))] flex-col"
		on:submit|preventDefault={saveWizard}
	>
		<header
			class="flex shrink-0 items-start justify-between border-b border-gray-100 px-5 py-4 dark:border-gray-800"
		>
			<div>
				<h2 class="text-lg font-semibold">{$i18n.t('Open WebUI setup wizard')}</h2>
				<p class="mt-1 text-xs text-gray-500">
					{$i18n.t('Set up the features you use most. You can change every option later.')}
				</p>
			</div>
			<button
				type="button"
				class="rounded-lg p-1.5 text-gray-400 transition hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-800 dark:hover:text-gray-200"
				on:click={() => (show = false)}
				aria-label={$i18n.t('Close')}
			>
				<XMark className="size-5" />
			</button>
		</header>

		{#if loading}
			<div class="flex min-h-80 flex-1 items-center justify-center">
				<Spinner className="size-5" />
			</div>
		{:else}
			<div class="min-h-0 flex-1 overflow-y-auto px-5">
				<section class="border-b border-gray-100 py-4 dark:border-gray-800">
					<div class="mb-2 text-sm font-medium">{$i18n.t('Primary use')}</div>
					<div
						class="grid grid-cols-1 overflow-hidden rounded-lg border border-gray-200 sm:grid-cols-3 dark:border-gray-700"
					>
						{#each [{ value: 'translation', label: $i18n.t('Translation purpose') }, { value: 'manuscript_translation', label: $i18n.t('Manuscript translation') }, { value: 'other', label: $i18n.t('Other uses') }] as option}
							<button
								type="button"
								class="px-3 py-2.5 text-sm transition {purpose === option.value
									? 'bg-gray-900 text-white dark:bg-white dark:text-black'
									: 'bg-white text-gray-600 hover:bg-gray-50 dark:bg-gray-900 dark:text-gray-300 dark:hover:bg-gray-800'}"
								on:click={() => selectPurpose(option.value as Purpose)}
							>
								{option.label}
							</button>
						{/each}
					</div>
				</section>

				{#if purpose === 'translation' || purpose === 'manuscript_translation'}
					<section class="border-b border-gray-100 py-4 dark:border-gray-800">
						<label class="block">
							<span class="text-sm font-medium">{$i18n.t('Translation languages')}</span>
							<SearchableSelect
								id="setup-glossary-selector"
								className="mt-2 w-full rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-850"
								bind:value={selectedGlossaryId}
								items={glossarySelectItems}
								placeholder={$i18n.t('Search')}
								emptyText={$i18n.t('No results found')}
							/>
						</label>

						{#if selectedGlossaryId === '__other__'}
							<div class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
								<label class="block">
									<span class="text-xs text-gray-500">{$i18n.t('Source language')}</span>
									<input
										class="mt-1 w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-hidden dark:border-gray-700 dark:bg-gray-850"
										bind:value={customSourceLanguage}
										required
									/>
								</label>
								<label class="block">
									<span class="text-xs text-gray-500">{$i18n.t('Target language')}</span>
									<input
										class="mt-1 w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-hidden dark:border-gray-700 dark:bg-gray-850"
										bind:value={customTargetLanguage}
										required
									/>
								</label>
								<label class="block sm:col-span-2">
									<span class="text-xs text-gray-500"
										>{$i18n.t('Glossary name')} ({$i18n.t('Optional')})</span
									>
									<input
										class="mt-1 w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-hidden dark:border-gray-700 dark:bg-gray-850"
										bind:value={customGlossaryName}
									/>
								</label>
							</div>
						{/if}
					</section>
				{/if}

				{#if purpose === 'translation'}
					<section
						class="flex items-center justify-between gap-4 border-b border-gray-100 py-4 dark:border-gray-800"
					>
						<div>
							<div class="text-sm font-medium">{$i18n.t('Speech recognition mode')}</div>
							<div class="mt-1 text-xs text-gray-500">
								{$user?.role === 'admin'
									? $i18n.t('This updates the administrator speech-to-text setting.')
									: $i18n.t(
											'Default follows the speech-to-text mode configured by the administrator.'
										)}
							</div>
						</div>
						<select
							class="min-w-36 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-hidden dark:border-gray-700 dark:bg-gray-850"
							bind:value={speechRecognitionMode}
						>
							{#if $user?.role === 'admin'}
								<option value="sherpa">Sherpa</option>
								<option value="multimodal">{$i18n.t('Multimodal')}</option>
							{:else}
								<option value="">{$i18n.t('Default')}</option>
								<option value="multimodal">{$i18n.t('Multimodal')}</option>
							{/if}
						</select>
					</section>
				{/if}

				<section
					class="flex items-center justify-between gap-4 border-b border-gray-100 py-4 dark:border-gray-800"
				>
					<div>
						<div class="text-sm font-medium">{$i18n.t('Default extension mode')}</div>
						<div class="mt-1 text-xs text-gray-500">
							{$i18n.t('Automatically enable this extension mode when Open WebUI starts.')}
						</div>
					</div>
					<select
						class="min-w-48 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm outline-hidden dark:border-gray-700 dark:bg-gray-850"
						bind:value={defaultExtensionMode}
					>
						{#each ['', 'translation', 'manuscript_translation', 'interpretation', 'learning', 'rag_translation'] as mode}
							<option value={mode}>{extensionModeLabel(mode as ExtensionMode)}</option>
						{/each}
					</select>
				</section>

				{#if $user?.role === 'admin'}
					<section class="border-b border-gray-100 py-4 dark:border-gray-800">
						<div class="space-y-3">
							<div class="flex items-center justify-between gap-4">
								<div class="text-sm font-medium">{$i18n.t('Context Compaction')}</div>
								<Switch bind:state={contextCompactionEnabled} />
							</div>

							<div class="flex items-center justify-between gap-4">
								<div class="text-sm font-medium">{$i18n.t('Memory Background Review')}</div>
								<Switch bind:state={memoryBackgroundReviewEnabled} />
							</div>
						</div>
					</section>
				{/if}

				<section
					class="flex items-start justify-between gap-4 border-b border-gray-100 py-4 dark:border-gray-800"
				>
					<div>
						<div class="text-sm font-medium">{$i18n.t('MTP acceleration')}</div>
						<div class="mt-1 max-w-xl text-xs leading-5 text-gray-500">
							{$i18n.t(
								'Improves response speed by 40%-150%, but uses roughly 1-2 GB more RAM or VRAM. On insufficient hardware, the gain may be small or performance may even decrease. Users with tight resources should leave it off.'
							)}
						</div>
					</div>
					<Switch bind:state={mtpEnabled} />
				</section>

				<section
					class="flex items-start justify-between gap-4 border-b border-gray-100 py-4 dark:border-gray-800"
				>
					<div>
						<div class="text-sm font-medium">{$i18n.t('Multimodal input')}</div>
						<div class="mt-1 max-w-xl text-xs leading-5 text-gray-500">
							{$i18n.t(
								'Supports image and video input and uses roughly 0.5-1 GB more RAM or VRAM when enabled. Leave it off on resource-constrained systems if you do not need it; enable it when needed. Some models also support audio input when multimodal is enabled.'
							)}
						</div>
					</div>
					<Switch bind:state={multimodalEnabled} />
				</section>

				<section class="flex items-start justify-between gap-4 py-4">
					<div>
						<div class="text-sm font-medium">{$i18n.t('Context size')}</div>
						<div class="mt-1 max-w-xl text-xs leading-5 text-gray-500">
							{$i18n.t(
								'Context size affects how much text can be sent. A value that is too small prevents long messages, while a larger value uses more memory. Most users should keep the default.'
							)}
						</div>
					</div>
					<input
						type="number"
						min="1"
						step="1"
						class="w-28 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-right text-sm outline-hidden dark:border-gray-700 dark:bg-gray-850"
						bind:value={contextSize}
					/>
				</section>
			</div>
		{/if}

		<footer class="shrink-0 border-t border-gray-100 px-5 py-4 dark:border-gray-800">
			<p class="mb-3 text-xs text-gray-500">
				{$i18n.t(
					'You can reopen this wizard later from your profile menu in the lower-left or upper-right corner.'
				)}
			</p>
			<div class="flex items-center justify-end gap-2">
				<button
					type="button"
					class="rounded-lg border border-gray-200 px-4 py-2 text-sm text-gray-600 transition hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
					on:click={skipWizard}
					disabled={saving}
				>
					{$i18n.t('Skip for now')}
				</button>
				<button
					type="submit"
					class="min-w-28 rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-black disabled:opacity-50 dark:bg-white dark:text-black dark:hover:bg-gray-100"
					disabled={loading || saving}
				>
					{#if saving}
						<span class="inline-flex items-center gap-2"
							><Spinner className="size-4" />{$i18n.t('Saving')}</span
						>
					{:else}
						{$i18n.t('Save and finish')}
					{/if}
				</button>
			</div>
		</footer>
	</form>
</Modal>
