<script lang="ts">
	import { getContext } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { validateOpenCodeDirectory, type OpenCodeChatConfig } from '$lib/apis/opencode';
	import FolderOpen from '$lib/components/icons/FolderOpen.svelte';
	const i18n = getContext('i18n');
	export let config: OpenCodeChatConfig;
	export let onChange: (value: OpenCodeChatConfig) => void | Promise<void>;
	export let disabled = false;
	let editing = false;
	let draft = '';
	let busy = false;
	const save = async (directory: string) => {
		if (!directory.trim() || busy) return;
		busy = true;
		try {
			const result = await validateOpenCodeDirectory(localStorage.token, directory.trim());
			await onChange({ ...config, directory: result.directory });
			editing = false;
		} catch (error) {
			toast.error(String(error));
		} finally {
			busy = false;
		}
	};
	const choose = async () => {
		if (window.electronAPI?.send) {
			try {
				const directory = await window.electronAPI.send({ type: 'selectFolder' });
				if (typeof directory === 'string' && directory) await save(directory);
			} catch (error) {
				toast.error(String(error));
			}
		} else {
			draft = config.directory;
			editing = !editing;
		}
	};
</script>

<div class="flex shrink-0 items-center gap-1.5 px-1 text-xs">
	<button
		type="button"
		on:click={choose}
		disabled={disabled || busy}
		class="flex max-w-48 items-center gap-1.5 rounded-full px-2 py-1.5 text-gray-600 hover:bg-gray-100 disabled:opacity-50 dark:text-gray-300 dark:hover:bg-gray-800"
		title={config.directory || $i18n.t('Choose working folder')}
		aria-label={$i18n.t('Choose working folder')}
	>
		<FolderOpen className="size-4 shrink-0" /><span class="truncate"
			>{config.directory.split(/[\\/]/).filter(Boolean).pop() ||
				$i18n.t('Choose working folder')}</span
		>
	</button>
	<select
		aria-label={$i18n.t('Agent mode')}
		{disabled}
		class="rounded-full border-0 bg-transparent px-2 py-1.5 text-xs text-gray-600 outline-hidden hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"
		value={config.agent || 'build'}
		title={config.agent === 'plan'
			? $i18n.t('Plan: read and propose changes only')
			: $i18n.t('Build: execute tasks and edit files')}
		on:change={(event) =>
			onChange({ ...config, agent: event.currentTarget.value as OpenCodeChatConfig['agent'] })}
	>
		<option value="build">Build</option>
		<option value="plan">Plan</option>
	</select>
	<select
		aria-label={$i18n.t('Approval rules')}
		{disabled}
		class="max-w-44 rounded-full border-0 bg-transparent px-2 py-1.5 text-xs text-gray-600 outline-hidden hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"
		class:text-orange-600={config.approval_mode === 'full'}
		value={config.approval_mode ?? 'task'}
		on:change={(event) =>
			onChange({
				...config,
				approval_mode: event.currentTarget.value as OpenCodeChatConfig['approval_mode']
			})}
	>
		<option value="task">{$i18n.t('Approve once per task')}</option>
		<option value="step">{$i18n.t('Confirm each action')}</option>
		<option value="full">{$i18n.t('Full control')}</option>
	</select>
	{#if editing}
		<div class="flex items-center gap-1">
			<input
				aria-label={$i18n.t('Working folder path')}
				class="w-40 rounded border bg-transparent px-2 py-1"
				bind:value={draft}
				placeholder={$i18n.t('Working folder path')}
				disabled={busy}
				on:keydown={(event) => {
					if (event.key === 'Enter') {
						event.preventDefault();
						event.stopPropagation();
						void save(draft);
					}
				}}
			/>
			<button
				type="button"
				on:click={() => save(draft)}
				disabled={busy}
				class="rounded px-2 py-1 hover:bg-gray-100 dark:hover:bg-gray-800">{$i18n.t('Save')}</button
			>
		</div>
	{/if}
</div>
