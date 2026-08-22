<script lang="ts">
	import { getContext, onMount, tick } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { goto } from '$app/navigation';
	import { fly } from 'svelte/transition';

	import {
		config,
		user,
		tools as _tools,
		skills as _skills,
		mobile,
		settings,
		toolServers,
		terminalServers,
		translationModeEnabled,
		interpretationModeEnabled,
		learningModeEnabled,
		manuscriptTranslationModeEnabled,
		ragTranslationModeEnabled,
		imageGenerationEnabled,
		openCodeModeEnabled,
		temporaryChatEnabled,
		codeInterpreterEnabled
	} from '$lib/stores';

	import { initiateOAuthRedirect } from '$lib/apis/configs';
	import { deleteOAuthSession } from '$lib/apis/auths';
	import { getTools } from '$lib/apis/tools';
	import { getSkills } from '$lib/apis/skills';
	import { getKnowledgeBases } from '$lib/apis/knowledge';
	import { updateUserSettings } from '$lib/apis/users';
	import {
		getOpenCodeCapabilities,
		getOpenCodeStatus,
		resetOpenCodeSession,
		validateOpenCodeDirectory,
		type OpenCodeCapabilities,
		type OpenCodeChatConfig,
		type OpenCodeStatus
	} from '$lib/apis/opencode';
	import { applyExtensionMode, type ExtensionMode } from '$lib/utils/extension-modes';
	import type {
		ConversationGlossaryConfig,
		GlossarySettings
	} from '$lib/utils/conversation-glossary';
	import ConversationGlossaryPicker from './ConversationGlossaryPicker.svelte';

	import { toast } from 'svelte-sonner';

	import Knobs from '$lib/components/icons/Knobs.svelte';
	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import Wrench from '$lib/components/icons/Wrench.svelte';
	import Keyframes from '$lib/components/icons/Keyframes.svelte';
	import Sparkles from '$lib/components/icons/Sparkles.svelte';
	import Photo from '$lib/components/icons/Photo.svelte';
	import Terminal from '$lib/components/icons/Terminal.svelte';
	import ChevronRight from '$lib/components/icons/ChevronRight.svelte';
	import ChevronLeft from '$lib/components/icons/ChevronLeft.svelte';
	import LinkSlash from '$lib/components/icons/LinkSlash.svelte';
	import BookOpen from '$lib/components/icons/BookOpen.svelte';
	import Bolt from '$lib/components/icons/Bolt.svelte';
	import Document from '$lib/components/icons/Document.svelte';
	import FolderOpen from '$lib/components/icons/FolderOpen.svelte';

	const i18n: Writable<i18nType> = getContext('i18n');

	export let chatId = '';
	export let selectedToolIds: string[] = [];
	export let selectedSkillIds: string[] = [];

	export let selectedModels: string[] = [];
	export let fileUploadCapableModels: string[] = [];

	export let toggleFilters: {
		id: string;
		name: string;
		description?: string;
		icon?: string;
		has_user_valves?: boolean;
	}[] = [];
	export let selectedFilterIds: string[] = [];

	export let showWebSearchButton = false;
	export let webSearchEnabled = false;
	export let showImageGenerationButton = true;
	export let showCodeInterpreterButton = true;
	export let showTranslationModeButton = true;
	export let glossarySettings: GlossarySettings | null = null;
	export let conversationGlossary: ConversationGlossaryConfig | null = null;
	export let onConversationGlossaryChange: (value: ConversationGlossaryConfig) => void = () => {};
	export let openCodeConfig: OpenCodeChatConfig = {
		enabled: false,
		directory: '',
		agent: 'build',
		model: ''
	};
	export let onOpenCodeConfigChange: (value: OpenCodeChatConfig) => void | Promise<void> = () => {};
	export let onOpenCodeSessionReset: () => void | Promise<void> = () => {};

	export let onShowValves: Function;
	export let onClose: Function;
	export let onWebSearchToggle: Function = () => {};
	export let closeOnOutsideClick = true;

	let show = false;
	let tab = '';
	let tools: Record<string, any> = {};
	let skills: Record<string, any> = {};

	let knowledgeBases: { id: string; name: string; type: string }[] = [];
	let knowledgeLoading = false;
	let openCodeStatus: OpenCodeStatus | null = null;
	let openCodeLoading = false;
	let openCodeError = '';
	let openCodeDirectoryDraft = '';
	let openCodeCapabilities: OpenCodeCapabilities | null = null;
	let openCodeCapabilityLoading = false;
	let openCodeSessionResetting = false;

	$: if (show) {
		init();
	}

	let fileUploadEnabled = true;
	$: fileUploadEnabled =
		fileUploadCapableModels.length === selectedModels.length &&
		($user?.role === 'admin' || $user?.permissions?.chat?.file_upload);

	const getOpenCodeError = (error: unknown): string => {
		if (typeof error === 'string') return error;
		if (error instanceof Error) return error.message;
		if (error && typeof error === 'object' && 'detail' in error) return String(error.detail);
		return $i18n.t('OpenCode request failed');
	};

	const loadOpenCodeCapabilities = async (directory: string, force = false) => {
		const candidate = directory.trim();
		if (
			!candidate ||
			openCodeCapabilityLoading ||
			(!force && openCodeCapabilities?.directory === candidate)
		)
			return;
		openCodeCapabilityLoading = true;
		try {
			const capabilities = await getOpenCodeCapabilities(localStorage.token, candidate);
			openCodeCapabilities = capabilities;
			const agent = capabilities.agents.some((item) => item.id === openCodeConfig.agent)
				? openCodeConfig.agent
				: (capabilities.agents[0]?.id ?? 'build');
			const model =
				!openCodeConfig.model ||
				capabilities.models.some((item) => item.id === openCodeConfig.model)
					? openCodeConfig.model
					: '';
			if (agent !== openCodeConfig.agent || model !== openCodeConfig.model) {
				await persistOpenCodeConfig({ ...openCodeConfig, agent, model });
			}
		} catch (error) {
			openCodeError = getOpenCodeError(error);
		} finally {
			openCodeCapabilityLoading = false;
		}
	};

	const refreshOpenCodeStatus = async () => {
		if (openCodeLoading) return;
		openCodeLoading = true;
		openCodeError = '';
		try {
			const status = await getOpenCodeStatus(localStorage.token, chatId || undefined);
			openCodeStatus = status;
			openCodeDirectoryDraft =
				openCodeConfig.directory || status.session?.directory || status.default_directory || '';
			if (!status.available) {
				openCodeError = status.error || $i18n.t('OpenCode is not running');
			} else if (openCodeDirectoryDraft) {
				await loadOpenCodeCapabilities(openCodeDirectoryDraft);
			}
		} catch (error) {
			openCodeStatus = { available: false };
			openCodeError = getOpenCodeError(error);
		} finally {
			openCodeLoading = false;
		}
	};

	const init = async () => {
		if ($_tools === null) {
			await _tools.set(await getTools(localStorage.token));
		}

		tools = {};
		if ($_tools) {
			tools = $_tools.reduce<Record<string, any>>((a, tool) => {
				a[tool.id] = {
					name: tool.name,
					description: tool.meta.description,
					enabled: selectedToolIds.includes(tool.id),
					...tool
				};
				return a;
			}, {});
		}

		if ($toolServers) {
			for (const serverIdx in $toolServers) {
				const server = $toolServers[serverIdx];
				if (server.info) {
					tools[`direct_server:${serverIdx}`] = {
						name: server?.info?.title ?? server.url,
						description: server.info.description ?? '',
						enabled: selectedToolIds.includes(`direct_server:${serverIdx}`)
					};
				}
			}
		}

		selectedToolIds = selectedToolIds.filter((id) => Object.keys(tools).includes(id));

		if ($_skills === null) {
			await _skills.set(await getSkills(localStorage.token));
		}

		skills = {};
		if ($_skills) {
			skills = $_skills
				.filter((skill) => skill.is_active)
				.reduce<Record<string, any>>((a, skill) => {
					a[skill.id] = {
						name: skill.name,
						description: skill.description,
						enabled: selectedSkillIds.includes(skill.id),
						...skill
					};
					return a;
				}, {});
		}

		selectedSkillIds = selectedSkillIds.filter((id) => Object.keys(skills ?? {}).includes(id));
		if ($user?.role === 'admin') {
			await refreshOpenCodeStatus();
		}
	};

	const loadKnowledgeBases = async () => {
		knowledgeLoading = true;
		try {
			const res = await getKnowledgeBases(localStorage.token).catch(() => {
				return null;
			});

			if (res) {
				const pageItems = Array.isArray(res.items) ? res.items : [];
				knowledgeBases = pageItems.map(
					(kb: { id: string; name: string; meta?: { knowledge_type?: string } }) => ({
						id: kb.id,
						name: kb.name,
						type: kb.meta?.knowledge_type ?? ''
					})
				);
			}
		} catch (e) {
			console.error('Failed to load knowledge bases:', e);
		} finally {
			knowledgeLoading = false;
		}
	};

	const openGlossarySettings = async () => {
		show = false;
		onClose?.();
		await goto('/glossary');
	};

	const setDefaultExtensionMode = async (event: Event, mode: ExtensionMode) => {
		event.stopPropagation();
		event.preventDefault();

		const nextSettings = { ...$settings, defaultExtensionMode: mode };
		try {
			await updateUserSettings(localStorage.token, { ui: nextSettings });
			settings.set(nextSettings);
			applyExtensionMode(mode);
			toast.success($i18n.t('Default extension mode updated'));
		} catch (error) {
			toast.error(error instanceof Error ? error.message : `${error}`);
		}
	};

	const persistOpenCodeConfig = async (value: OpenCodeChatConfig) => {
		openCodeConfig = value;
		await onOpenCodeConfigChange(value);
	};

	const validateAndSaveOpenCodeDirectory = async (
		directory: string,
		enabled = openCodeConfig.enabled
	): Promise<string | null> => {
		const candidate = directory.trim();
		if (!candidate) {
			openCodeError = $i18n.t('Select a project directory');
			toast.error(openCodeError);
			return null;
		}
		openCodeLoading = true;
		openCodeError = '';
		try {
			const result = await validateOpenCodeDirectory(localStorage.token, candidate);
			const nextConfig: OpenCodeChatConfig = {
				...openCodeConfig,
				directory: result.directory,
				enabled
			};
			await persistOpenCodeConfig(nextConfig);
			openCodeDirectoryDraft = result.directory;
			await loadOpenCodeCapabilities(result.directory, true);
			return result.directory;
		} catch (error) {
			openCodeError = getOpenCodeError(error);
			toast.error(openCodeError);
			return null;
		} finally {
			openCodeLoading = false;
		}
	};

	const pickOpenCodeDirectory = async (): Promise<string | null> => {
		if (!window.electronAPI?.send) {
			toast.info($i18n.t('Enter the project directory path'));
			document.getElementById('opencode-project-directory')?.focus();
			return null;
		}
		try {
			const directory = await window.electronAPI.send({ type: 'selectFolder' });
			if (typeof directory === 'string' && directory) {
				openCodeDirectoryDraft = directory;
				return directory;
			}
		} catch (error) {
			openCodeError = getOpenCodeError(error);
			toast.error(openCodeError);
		}
		return null;
	};

	const toggleOpenCode = async () => {
		if ($openCodeModeEnabled || openCodeConfig.enabled) {
			applyExtensionMode('');
			await persistOpenCodeConfig({ ...openCodeConfig, enabled: false });
			return;
		}

		if ($temporaryChatEnabled) {
			toast.error($i18n.t('Code mode requires a saved conversation'));
			return;
		}

		if (openCodeStatus === null) {
			await refreshOpenCodeStatus();
		}
		if (!openCodeStatus?.available) {
			toast.error(openCodeError || $i18n.t('OpenCode is not running'));
			return;
		}

		let directory =
			openCodeDirectoryDraft || openCodeConfig.directory || openCodeStatus.default_directory || '';
		if (!directory) {
			directory = (await pickOpenCodeDirectory()) || '';
		}
		if (!directory) return;

		const validatedDirectory = await validateAndSaveOpenCodeDirectory(directory, true);
		if (!validatedDirectory) return;
		applyExtensionMode('code');
	};

	const setOpenCodeAgent = async (agent: OpenCodeChatConfig['agent']) => {
		await persistOpenCodeConfig({
			...openCodeConfig,
			agent
		});
	};

	const setOpenCodeModel = async (model: string) => {
		await persistOpenCodeConfig({
			...openCodeConfig,
			model
		});
	};

	const resetOpenCodeAgentSession = async () => {
		if (!chatId || !openCodeStatus?.session?.id || openCodeSessionResetting) return;
		if (!confirm($i18n.t('Start a new Agent session?'))) return;
		openCodeSessionResetting = true;
		try {
			const result = await resetOpenCodeSession(localStorage.token, chatId);
			if (!result.reset) throw new Error($i18n.t('Failed to reset Agent session'));
			await onOpenCodeSessionReset();
			openCodeStatus = {
				...openCodeStatus,
				session: { ...openCodeStatus.session, id: null }
			};
			toast.success($i18n.t('Agent session reset'));
		} catch (error) {
			toast.error(getOpenCodeError(error));
		} finally {
			openCodeSessionResetting = false;
		}
	};

	const disableOpenCode = async () => {
		if (!$openCodeModeEnabled && !openCodeConfig.enabled) return;
		openCodeModeEnabled.set(false);
		await persistOpenCodeConfig({ ...openCodeConfig, enabled: false });
	};

	const disableGlossaryModesExcept = (
		mode: 'translation' | 'manuscript' | 'interpretation' | 'learning' | 'rag'
	) => {
		void disableOpenCode();
		if (mode !== 'translation') translationModeEnabled.set(false);
		if (mode !== 'manuscript') manuscriptTranslationModeEnabled.set(false);
		if (mode !== 'interpretation') interpretationModeEnabled.set(false);
		if (mode !== 'learning') learningModeEnabled.set(false);
		if (mode !== 'rag') {
			ragTranslationModeEnabled.set(false);
		}
	};

	const openKnowledgeSettings = async () => {
		show = false;
		onClose?.();
		await goto('/workspace/knowledge');
	};
</script>

<Dropdown
	bind:show
	onOpenChange={(state) => {
		if (state === false) {
			onClose();
		}
	}}
>
	<Tooltip content={$i18n.t('Integrations')} placement="top">
		<slot />
	</Tooltip>
	<div slot="content">
		<div
			class="min-w-70 max-w-70 rounded-2xl px-1 py-1 border border-gray-100 dark:border-gray-800 z-50 bg-white dark:bg-gray-850 dark:text-white shadow-lg max-h-72 overflow-y-auto overflow-x-hidden scrollbar-thin"
		>
			{#if tab === ''}
				<div in:fly={{ x: -20, duration: 150 }}>
					{#if tools}
						{#if Object.keys(tools).length > 0}
							<button
								class="flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
								on:click={() => {
									tab = 'tools';
								}}
							>
								<Wrench />

								<div class="flex items-center w-full justify-between">
									<div class=" line-clamp-1">
										{$i18n.t('Tools')}
										<span class="ml-0.5 text-gray-500">{Object.keys(tools).length}</span>
									</div>

									<div class="text-gray-500">
										<ChevronRight />
									</div>
								</div>
							</button>
						{/if}

						{#if skills && Object.keys(skills).length > 0}
							<button
								class="flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
								on:click={() => {
									tab = 'skills';
								}}
							>
								<Keyframes className="size-4" strokeWidth="1.75" />

								<div class="flex items-center w-full justify-between">
									<div class=" line-clamp-1">
										{$i18n.t('Skills')}
										<span class="ml-0.5 text-gray-500">{Object.keys(skills).length}</span>
									</div>

									<div class="text-gray-500">
										<ChevronRight />
									</div>
								</div>
							</button>
						{/if}
					{:else}
						<div class="py-4">
							<Spinner />
						</div>
					{/if}

					{#if toggleFilters && toggleFilters.length > 0}
						{#each toggleFilters.sort( (a, b) => a.name.localeCompare( b.name, undefined, { sensitivity: 'base' } ) ) as filter, filterIdx (filter.id)}
							<Tooltip content={filter?.description} placement="top-start">
								<button
									class="flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
									on:click={() => {
										if (selectedFilterIds.includes(filter.id)) {
											selectedFilterIds = selectedFilterIds.filter((id) => id !== filter.id);
										} else {
											selectedFilterIds = [...selectedFilterIds, filter.id];
										}
									}}
								>
									<div class="flex-1 truncate">
										<div class="flex flex-1 gap-2 items-center">
											<div class="shrink-0">
												{#if filter?.icon}
													<div class="size-4 items-center flex justify-center">
														<img
															src={filter.icon}
															class="size-3.5 {filter.icon.includes('data:image/svg')
																? 'dark:invert-[80%]'
																: ''}"
															style="fill: currentColor;"
															alt={filter.name}
														/>
													</div>
												{:else}
													<Sparkles className="size-4" strokeWidth="1.75" />
												{/if}
											</div>

											<div class=" truncate">{filter?.name}</div>
										</div>
									</div>

									{#if filter?.has_user_valves && ($user?.role === 'admin' || ($user?.permissions?.chat?.valves ?? true))}
										<div class=" shrink-0">
											<Tooltip content={$i18n.t('Valves')}>
												<button
													class="self-center w-fit text-sm text-gray-600 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition rounded-full"
													type="button"
													on:click={(e) => {
														e.stopPropagation();
														e.preventDefault();
														onShowValves({
															type: 'function',
															id: filter.id
														});
													}}
												>
													<Knobs />
												</button>
											</Tooltip>
										</div>
									{/if}

									<div class=" shrink-0">
										<Switch
											state={selectedFilterIds.includes(filter.id)}
											on:change={async (e) => {
												const state = e.detail;
												await tick();
											}}
										/>
									</div>
								</button>
							</Tooltip>
						{/each}
					{/if}

					{#if $user?.role === 'admin'}
						<Tooltip content={$i18n.t('Use OpenCode as a coding agent')} placement="top-start">
							<button
								class="flex w-full items-center justify-between gap-2 rounded-xl px-3 py-1.5 text-sm hover:bg-gray-50 dark:hover:bg-gray-800/50"
								aria-pressed={$openCodeModeEnabled}
								disabled={openCodeLoading}
								on:click={() => void toggleOpenCode()}
							>
								<div class="flex min-w-0 flex-1 items-center gap-2">
									<Terminal className="size-3.5 shrink-0" strokeWidth="1.75" />
									<span class="truncate">{$i18n.t('Code Mode')}</span>
									<span
										class="truncate text-[10px] {openCodeStatus?.available
											? 'text-green-600 dark:text-green-400'
											: 'text-gray-400 dark:text-gray-500'}"
									>
										{openCodeStatus?.available
											? (openCodeStatus.version ?? $i18n.t('Ready'))
											: $i18n.t('Not running')}
									</span>
								</div>
								{#if openCodeLoading}
									<Spinner className="size-4" />
								{:else}
									<Switch state={$openCodeModeEnabled} on:change={async () => await tick()} />
								{/if}
							</button>
						</Tooltip>

						{#if $openCodeModeEnabled}
							<div
								class="mx-2 mb-2 space-y-2 border-l-2 border-gray-200 px-3 py-1.5 dark:border-gray-700"
							>
								{#if openCodeError}
									<div class="text-xs text-red-600 dark:text-red-400">{openCodeError}</div>
								{/if}
								<label
									class="block text-[11px] font-medium text-gray-500 dark:text-gray-400"
									for="opencode-project-directory">{$i18n.t('Project directory')}</label
								>
								<div class="flex items-center gap-1.5">
									<input
										id="opencode-project-directory"
										class="min-w-0 flex-1 rounded-md border border-gray-200 bg-transparent px-2 py-1.5 text-xs outline-hidden focus:border-gray-400 dark:border-gray-700"
										bind:value={openCodeDirectoryDraft}
										placeholder={$i18n.t('Select a project directory')}
										on:keydown={(event) => {
											if (event.key === 'Enter') {
												event.preventDefault();
												void validateAndSaveOpenCodeDirectory(openCodeDirectoryDraft);
											}
										}}
										on:blur={() => {
											if (openCodeDirectoryDraft.trim() !== openCodeConfig.directory)
												void validateAndSaveOpenCodeDirectory(openCodeDirectoryDraft);
										}}
									/>
									<Tooltip content={$i18n.t('Choose project folder')}>
										<button
											type="button"
											class="flex size-8 shrink-0 items-center justify-center rounded-md hover:bg-gray-100 dark:hover:bg-gray-800"
											on:click={async () => {
												const directory = await pickOpenCodeDirectory();
												if (directory) await validateAndSaveOpenCodeDirectory(directory);
											}}
										>
											<FolderOpen className="size-4" strokeWidth="1.75" />
										</button>
									</Tooltip>
								</div>
								<div class="grid grid-cols-2 gap-2">
									<label class="min-w-0 text-[11px] text-gray-500 dark:text-gray-400">
										<span>{$i18n.t('Coding agent')}</span>
										<select
											class="mt-1 w-full rounded-md border border-gray-200 bg-transparent px-2 py-1.5 text-xs text-gray-900 outline-hidden focus:border-gray-400 dark:border-gray-700 dark:text-gray-100"
											value={openCodeConfig.agent}
											on:change={(event) =>
												void setOpenCodeAgent((event.currentTarget as HTMLSelectElement).value)}
										>
											{#if !openCodeCapabilities?.agents.some((item) => item.id === openCodeConfig.agent)}
												<option value={openCodeConfig.agent}>{openCodeConfig.agent}</option>
											{/if}
											{#each openCodeCapabilities?.agents ?? [{ id: 'build', name: 'build' }, { id: 'plan', name: 'plan' }] as agent}
												<option value={agent.id}>{agent.name}</option>
											{/each}
										</select>
									</label>
									<label class="min-w-0 text-[11px] text-gray-500 dark:text-gray-400">
										<span>{$i18n.t('Model')}</span>
										<select
											class="mt-1 w-full rounded-md border border-gray-200 bg-transparent px-2 py-1.5 text-xs text-gray-900 outline-hidden focus:border-gray-400 dark:border-gray-700 dark:text-gray-100"
											value={openCodeConfig.model}
											on:change={(event) =>
												void setOpenCodeModel((event.currentTarget as HTMLSelectElement).value)}
										>
											<option value="">{$i18n.t('OpenCode default')}</option>
											{#if openCodeConfig.model && !openCodeCapabilities?.models.some((item) => item.id === openCodeConfig.model)}
												<option value={openCodeConfig.model}>{openCodeConfig.model}</option>
											{/if}
											{#each openCodeCapabilities?.models ?? [] as model}
												<option value={model.id}>{model.provider_name} · {model.name}</option>
											{/each}
										</select>
									</label>
								</div>
								{#if openCodeCapabilityLoading}
									<div class="flex items-center gap-2 text-[11px] text-gray-500">
										<Spinner className="size-3.5" />
										<span>{$i18n.t('Loading coding models')}</span>
									</div>
								{:else if openCodeCapabilities && openCodeCapabilities.models.length === 0}
									<div class="text-[11px] text-gray-500 dark:text-gray-400">
										{$i18n.t('No connected coding models')}
									</div>
								{/if}
								{#if openCodeCapabilities?.vcs.branch}
									<div class="truncate text-[11px] text-gray-500 dark:text-gray-400">
										{$i18n.t('Branch')}:
										<span class="font-mono">{openCodeCapabilities.vcs.branch}</span>
									</div>
								{/if}
								{#if openCodeStatus?.session?.id}
									<button
										type="button"
										class="text-left text-xs text-gray-600 hover:text-gray-900 disabled:opacity-50 dark:text-gray-400 dark:hover:text-gray-100"
										disabled={openCodeSessionResetting}
										on:click={() => void resetOpenCodeAgentSession()}
									>
										{openCodeSessionResetting
											? $i18n.t('Resetting Agent session')
											: $i18n.t('New Agent session')}
									</button>
								{/if}
							</div>
						{/if}
					{/if}

					<Tooltip
						content={$i18n.t('Use bilingual knowledge base to assist translation')}
						placement="top-start"
					>
						<button
							class="group flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
							on:click={() => {
								ragTranslationModeEnabled.update((v) => !v);
								if ($ragTranslationModeEnabled) {
									disableGlossaryModesExcept('rag');
								}
							}}
						>
							<div class="flex-1 truncate">
								<div class="flex flex-1 gap-2 items-center">
									<div class="shrink-0">
										<svg
											xmlns="http://www.w3.org/2000/svg"
											class="size-4"
											fill="none"
											viewBox="0 0 24 24"
											stroke="currentColor"
											stroke-width="1.75"
										>
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												d="M3 5h12M9 3v2m1.048 9.5A18.022 18.022 0 016.412 9m6.088 9h7M11 21l5-10 5 10M12.751 5C11.783 10.77 8.07 15.61 3 18.129"
											/>
										</svg>
									</div>
									<div class="truncate">{$i18n.t('RAG Translation Mode')}</div>
								</div>
							</div>

							<span
								role="button"
								tabindex="0"
								class="shrink-0 text-[11px] transition {$settings?.defaultExtensionMode ===
								'rag_translation'
									? 'text-blue-600 opacity-100 dark:text-blue-400'
									: 'text-gray-500 opacity-0 group-hover:opacity-100 focus:opacity-100 dark:text-gray-400'}"
								on:click={(event) => setDefaultExtensionMode(event, 'rag_translation')}
								on:keydown={(event) => {
									if (event.key === 'Enter' || event.key === ' ') {
										void setDefaultExtensionMode(event, 'rag_translation');
									}
								}}
							>
								{$i18n.t(
									$settings?.defaultExtensionMode === 'rag_translation'
										? 'Default'
										: 'Set as default'
								)}
							</span>

							<div class="shrink-0">
								<Tooltip content={$i18n.t('Knowledge base settings')}>
									<button
										class="self-center w-fit text-sm text-gray-600 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition rounded-full"
										type="button"
										on:click={(e) => {
											e.stopPropagation();
											e.preventDefault();
											openKnowledgeSettings();
										}}
									>
										<Knobs />
									</button>
								</Tooltip>
							</div>

							<div class="shrink-0">
								<Switch
									state={$ragTranslationModeEnabled}
									on:change={async () => {
										await tick();
									}}
								/>
							</div>
						</button>
					</Tooltip>

					{#if showTranslationModeButton}
						<Tooltip content={$i18n.t('Glossary-assisted translation')} placement="top-start">
							<button
								class="group flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
								on:click={() => {
									translationModeEnabled.update((value) => !value);
									if ($translationModeEnabled) {
										disableGlossaryModesExcept('translation');
									}
								}}
							>
								<div class="flex-1 truncate">
									<div class="flex flex-1 gap-2 items-center">
										<div class="shrink-0">
											<BookOpen className="size-4" strokeWidth="1.75" />
										</div>

										<div class=" truncate">{$i18n.t('Translation Mode')}</div>
									</div>
								</div>

								<span
									role="button"
									tabindex="0"
									class="shrink-0 text-[11px] transition {$settings?.defaultExtensionMode ===
									'translation'
										? 'text-blue-600 opacity-100 dark:text-blue-400'
										: 'text-gray-500 opacity-0 group-hover:opacity-100 focus:opacity-100 dark:text-gray-400'}"
									on:click={(event) => setDefaultExtensionMode(event, 'translation')}
									on:keydown={(event) => {
										if (event.key === 'Enter' || event.key === ' ') {
											void setDefaultExtensionMode(event, 'translation');
										}
									}}
								>
									{$i18n.t(
										$settings?.defaultExtensionMode === 'translation' ? 'Default' : 'Set as default'
									)}
								</span>

								<div class=" shrink-0">
									<Tooltip content={$i18n.t('Glossary settings')}>
										<button
											class="self-center w-fit text-sm text-gray-600 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition rounded-full"
											type="button"
											on:click={(e) => {
												e.stopPropagation();
												e.preventDefault();
												openGlossarySettings();
											}}
										>
											<Knobs />
										</button>
									</Tooltip>
								</div>

								<div class=" shrink-0">
									<Switch
										state={$translationModeEnabled}
										on:change={async () => {
											await tick();
										}}
									/>
								</div>
							</button>
						</Tooltip>

						<Tooltip
							content={$i18n.t('Glossary-assisted manuscript translation')}
							placement="top-start"
						>
							<button
								class="group flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
								on:click={() => {
									manuscriptTranslationModeEnabled.update((v) => !v);
									if ($manuscriptTranslationModeEnabled) {
										disableGlossaryModesExcept('manuscript');
									}
								}}
							>
								<div class="flex-1 truncate">
									<div class="flex flex-1 gap-2 items-center">
										<div class="shrink-0">
											<Document className="size-4" strokeWidth="1.75" />
										</div>

										<div class=" truncate">{$i18n.t('Manuscript Translation Mode')}</div>
									</div>
								</div>

								<span
									role="button"
									tabindex="0"
									class="shrink-0 text-[11px] transition {$settings?.defaultExtensionMode ===
									'manuscript_translation'
										? 'text-blue-600 opacity-100 dark:text-blue-400'
										: 'text-gray-500 opacity-0 group-hover:opacity-100 focus:opacity-100 dark:text-gray-400'}"
									on:click={(event) => setDefaultExtensionMode(event, 'manuscript_translation')}
									on:keydown={(event) => {
										if (event.key === 'Enter' || event.key === ' ') {
											void setDefaultExtensionMode(event, 'manuscript_translation');
										}
									}}
								>
									{$i18n.t(
										$settings?.defaultExtensionMode === 'manuscript_translation'
											? 'Default'
											: 'Set as default'
									)}
								</span>

								<div class=" shrink-0">
									<Tooltip content={$i18n.t('Glossary settings')}>
										<button
											class="self-center w-fit text-sm text-gray-600 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition rounded-full"
											type="button"
											on:click={(e) => {
												e.stopPropagation();
												e.preventDefault();
												openGlossarySettings();
											}}
										>
											<Knobs />
										</button>
									</Tooltip>
								</div>

								<div class=" shrink-0">
									<Switch
										state={$manuscriptTranslationModeEnabled}
										on:change={async () => {
											await tick();
										}}
									/>
								</div>
							</button>
						</Tooltip>

						<Tooltip
							content={$i18n.t('Glossary-assisted simultaneous interpretation')}
							placement="top-start"
						>
							<button
								class="group flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
								on:click={() => {
									interpretationModeEnabled.update((v) => !v);
									if ($interpretationModeEnabled) {
										disableGlossaryModesExcept('interpretation');
									}
								}}
							>
								<div class="flex-1 truncate">
									<div class="flex flex-1 gap-2 items-center">
										<div class="shrink-0">
											<Bolt className="size-4" strokeWidth="1.75" />
										</div>

										<div class=" truncate">{$i18n.t('Simultaneous Interpretation')}</div>
									</div>
								</div>

								<span
									role="button"
									tabindex="0"
									class="shrink-0 text-[11px] transition {$settings?.defaultExtensionMode ===
									'interpretation'
										? 'text-blue-600 opacity-100 dark:text-blue-400'
										: 'text-gray-500 opacity-0 group-hover:opacity-100 focus:opacity-100 dark:text-gray-400'}"
									on:click={(event) => setDefaultExtensionMode(event, 'interpretation')}
									on:keydown={(event) => {
										if (event.key === 'Enter' || event.key === ' ') {
											void setDefaultExtensionMode(event, 'interpretation');
										}
									}}
								>
									{$i18n.t(
										$settings?.defaultExtensionMode === 'interpretation'
											? 'Default'
											: 'Set as default'
									)}
								</span>

								<div class=" shrink-0">
									<Tooltip content={$i18n.t('Glossary settings')}>
										<button
											class="self-center w-fit text-sm text-gray-600 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition rounded-full"
											type="button"
											on:click={(e) => {
												e.stopPropagation();
												e.preventDefault();
												openGlossarySettings();
											}}
										>
											<Knobs />
										</button>
									</Tooltip>
								</div>

								<div class=" shrink-0">
									<Switch
										state={$interpretationModeEnabled}
										on:change={async () => {
											await tick();
										}}
									/>
								</div>
							</button>
						</Tooltip>

						<Tooltip content={$i18n.t('Glossary-assisted language learning')} placement="top-start">
							<button
								class="group flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
								on:click={() => {
									learningModeEnabled.update((v) => !v);
									if ($learningModeEnabled) {
										disableGlossaryModesExcept('learning');
									}
								}}
							>
								<div class="flex-1 truncate">
									<div class="flex flex-1 gap-2 items-center">
										<div class="shrink-0">
											<BookOpen className="size-4" strokeWidth="1.75" />
										</div>

										<div class=" truncate">{$i18n.t('Learning Mode')}</div>
									</div>
								</div>

								<span
									role="button"
									tabindex="0"
									class="shrink-0 text-[11px] transition {$settings?.defaultExtensionMode ===
									'learning'
										? 'text-blue-600 opacity-100 dark:text-blue-400'
										: 'text-gray-500 opacity-0 group-hover:opacity-100 focus:opacity-100 dark:text-gray-400'}"
									on:click={(event) => setDefaultExtensionMode(event, 'learning')}
									on:keydown={(event) => {
										if (event.key === 'Enter' || event.key === ' ') {
											void setDefaultExtensionMode(event, 'learning');
										}
									}}
								>
									{$i18n.t(
										$settings?.defaultExtensionMode === 'learning' ? 'Default' : 'Set as default'
									)}
								</span>

								<div class=" shrink-0">
									<Tooltip content={$i18n.t('Glossary settings')}>
										<button
											class="self-center w-fit text-sm text-gray-600 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition rounded-full"
											type="button"
											on:click={(e) => {
												e.stopPropagation();
												e.preventDefault();
												openGlossarySettings();
											}}
										>
											<Knobs />
										</button>
									</Tooltip>
								</div>

								<div class=" shrink-0">
									<Switch
										state={$learningModeEnabled}
										on:change={async () => {
											await tick();
										}}
									/>
								</div>
							</button>
						</Tooltip>
					{/if}

					{#if $translationModeEnabled || $manuscriptTranslationModeEnabled || $interpretationModeEnabled || $learningModeEnabled || $ragTranslationModeEnabled}
						<ConversationGlossaryPicker
							{glossarySettings}
							{conversationGlossary}
							onChange={onConversationGlossaryChange}
						/>
					{/if}

					{#if showImageGenerationButton}
						<Tooltip content={$i18n.t('Generate an image')} placement="top-start">
							<button
								class="flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
								aria-pressed={$imageGenerationEnabled}
								aria-label={$imageGenerationEnabled
									? $i18n.t('Disable Image Generation')
									: $i18n.t('Enable Image Generation')}
								on:click={() => {
									imageGenerationEnabled.update((v) => !v);
								}}
							>
								<div class="flex-1 truncate">
									<div class="flex flex-1 gap-2 items-center">
										<div class="shrink-0">
											<Photo className="size-4" strokeWidth="1.5" />
										</div>

										<div class=" truncate">{$i18n.t('Image')}</div>
									</div>
								</div>

								<div class=" shrink-0">
									<Switch
										state={$imageGenerationEnabled}
										on:change={async () => {
											await tick();
										}}
									/>
								</div>
							</button>
						</Tooltip>
					{/if}

					{#if showCodeInterpreterButton}
						<Tooltip content={$i18n.t('Execute code for analysis')} placement="top-start">
							<button
								class="flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
								aria-pressed={$codeInterpreterEnabled}
								aria-label={$codeInterpreterEnabled
									? $i18n.t('Disable Code Interpreter')
									: $i18n.t('Enable Code Interpreter')}
								on:click={() => {
									codeInterpreterEnabled.update((v) => !v);
								}}
							>
								<div class="flex-1 truncate">
									<div class="flex flex-1 gap-2 items-center">
										<div class="shrink-0">
											<Terminal className="size-3.5" strokeWidth="1.75" />
										</div>

										<div class=" truncate">{$i18n.t('Code Interpreter')}</div>
									</div>
								</div>

								<div class=" shrink-0">
									<Switch
										state={$codeInterpreterEnabled}
										on:change={async () => {
											await tick();
										}}
									/>
								</div>
							</button>
						</Tooltip>
					{/if}
				</div>
			{:else if tab === 'tools' && tools}
				<div in:fly={{ x: 20, duration: 150 }}>
					<button
						class="flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
						on:click={() => {
							tab = '';
						}}
					>
						<ChevronLeft />

						<div class="flex items-center w-full justify-between">
							<div>
								{$i18n.t('Tools')}
								<span class="ml-0.5 text-gray-500">{Object.keys(tools).length}</span>
							</div>
						</div>
					</button>

					{#each Object.keys(tools) as toolId}
						<button
							class="relative flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
							on:click={async (e) => {
								if (!(tools[toolId]?.authenticated ?? true)) {
									e.preventDefault();

									const parts = toolId.split(':');
									initiateOAuthRedirect({
										id: toolId,
										serverId: parts.at(-1) ?? toolId,
										authType:
											parts.length > 1 ? (parts[0] === 'server' ? parts[1] : parts[0]) : null
									});
								} else {
									tools[toolId].enabled = !tools[toolId].enabled;

									const state = tools[toolId].enabled;
									await tick();

									if (state) {
										selectedToolIds = [...selectedToolIds, toolId];
									} else {
										selectedToolIds = selectedToolIds.filter((id) => id !== toolId);
									}
								}
							}}
						>
							{#if !(tools[toolId]?.authenticated ?? true)}
								<!-- make it slighly darker and not clickable -->
								<div class="absolute inset-0 opacity-50 rounded-xl cursor-pointer z-10" />
							{/if}
							<div class="flex-1 truncate">
								<div class="flex flex-1 gap-2 items-center">
									<Tooltip content={tools[toolId]?.name ?? ''} placement="top">
										<div class="shrink-0">
											<Wrench />
										</div>
									</Tooltip>
									<Tooltip content={tools[toolId]?.description ?? ''} placement="top-start">
										<div class=" truncate">{tools[toolId].name}</div>
									</Tooltip>
								</div>
							</div>

							{#if (tools[toolId]?.authenticated ?? true) && toolId.startsWith('server:mcp:')}
								<div class="shrink-0">
									<Tooltip content={$i18n.t('Disconnect OAuth')}>
										<button
											class="self-center w-fit text-sm text-gray-600 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition rounded-full"
											type="button"
											on:click={async (e) => {
												e.stopPropagation();
												e.preventDefault();

												const parts = toolId.split(':');
												const serverId = parts.at(-1) ?? toolId;
												const provider = `mcp:${serverId}`;

												try {
													await deleteOAuthSession(localStorage.token, provider);
													toast.success($i18n.t('OAuth session disconnected'));

													// Refresh tools to update authenticated state
													_tools.set(await getTools(localStorage.token));
													selectedToolIds = selectedToolIds.filter((id) => id !== toolId);
													await init();
												} catch (err) {
													toast.error(
														err instanceof Error
															? err.message
															: `${err ?? $i18n.t('Failed to disconnect')}`
													);
												}
											}}
										>
											<LinkSlash className="size-3.5" />
										</button>
									</Tooltip>
								</div>
							{/if}

							{#if tools[toolId]?.has_user_valves && ($user?.role === 'admin' || ($user?.permissions?.chat?.valves ?? true))}
								<div class=" shrink-0">
									<Tooltip content={$i18n.t('Valves')}>
										<button
											class="self-center w-fit text-sm text-gray-600 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition rounded-full"
											type="button"
											on:click={(e) => {
												e.stopPropagation();
												e.preventDefault();
												onShowValves({
													type: 'tool',
													id: toolId
												});
											}}
										>
											<Knobs />
										</button>
									</Tooltip>
								</div>
							{/if}

							<div class=" shrink-0">
								<Switch state={tools[toolId].enabled} />
							</div>
						</button>
					{/each}
				</div>
			{:else if tab === 'skills' && skills}
				<div in:fly={{ x: 20, duration: 150 }}>
					<button
						class="flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
						on:click={() => {
							tab = '';
						}}
					>
						<ChevronLeft />

						<div class="flex items-center w-full justify-between">
							<div>
								{$i18n.t('Skills')}
								<span class="ml-0.5 text-gray-500">{Object.keys(skills).length}</span>
							</div>
						</div>
					</button>

					{#each Object.keys(skills) as skillId}
						<button
							class="relative flex w-full justify-between gap-2 items-center px-3 py-1.5 text-sm cursor-pointer rounded-xl hover:bg-gray-50 dark:hover:bg-gray-800/50"
							on:click={async () => {
								skills[skillId].enabled = !skills[skillId].enabled;

								const state = skills[skillId].enabled;
								await tick();

								if (state) {
									selectedSkillIds = [...selectedSkillIds, skillId];
								} else {
									selectedSkillIds = selectedSkillIds.filter((id) => id !== skillId);
								}
							}}
						>
							<div class="flex-1 truncate">
								<div class="flex flex-1 gap-2 items-center">
									<Tooltip content={skills[skillId]?.name ?? ''} placement="top">
										<div class="shrink-0">
											<Keyframes className="size-4" strokeWidth="1.75" />
										</div>
									</Tooltip>
									<Tooltip content={skills[skillId]?.description ?? ''} placement="top-start">
										<div class=" truncate">{skills[skillId].name}</div>
									</Tooltip>
								</div>
							</div>

							<div class=" shrink-0">
								<Switch state={skills[skillId].enabled} />
							</div>
						</button>
					{/each}
				</div>
			{/if}
		</div>
	</div>
</Dropdown>
