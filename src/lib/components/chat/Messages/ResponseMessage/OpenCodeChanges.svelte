<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	import {
		getOpenCodeWorkspace,
		revertOpenCodeMessage,
		unrevertOpenCodeChat,
		type OpenCodeTodo,
		type OpenCodeVcs
	} from '$lib/apis/opencode';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import CodeBracket from '$lib/components/icons/CodeBracket.svelte';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import ChevronUp from '$lib/components/icons/ChevronUp.svelte';

	const i18n: Writable<i18nType> = getContext('i18n');

	export let chatId = '';
	export let messageId = '';
	export let count = 0;
	export let agent = '';
	export let model = '';
	export let todos: OpenCodeTodo[] = [];
	export let vcs: OpenCodeVcs = { branch: '', root: '' };

	let expanded = false;
	let loading = false;
	let actionLoading = false;
	let error = '';
	let actionMessage = '';
	let reverted = false;
	let items: Record<string, unknown>[] | null = null;
	let currentTodos = todos;
	let currentVcs = vcs;
	let currentAgent = agent;
	let currentModel = model;

	$: if (items === null) {
		currentTodos = todos;
		currentVcs = vcs;
		currentAgent = agent;
		currentModel = model;
	}

	const getFileName = (item: Record<string, unknown>): string =>
		String(item.file ?? item.path ?? item.filename ?? $i18n.t('Changed file'));

	const getNumber = (value: unknown): number => {
		const parsed = Number(value);
		return Number.isFinite(parsed) ? parsed : 0;
	};

	const getPreview = (item: Record<string, unknown>): string => {
		for (const key of ['patch', 'diff', 'content']) {
			if (typeof item[key] === 'string' && item[key]) return item[key] as string;
		}
		return '';
	};

	const getError = (value: unknown, fallback: string): string => {
		if (typeof value === 'string') return value;
		if (value instanceof Error) return value.message;
		return fallback;
	};

	const load = async (force = false) => {
		if ((!force && items !== null) || loading || !chatId) return;
		loading = true;
		error = '';
		try {
			const result = await getOpenCodeWorkspace(localStorage.token, chatId, messageId);
			items = result.diffs ?? [];
			currentTodos = result.todos ?? [];
			currentVcs = result.vcs ?? { branch: '', root: '' };
			currentAgent = result.agent || agent;
			currentModel = result.model || model;
		} catch (value) {
			error = getError(value, $i18n.t('Failed to load Agent workspace'));
		} finally {
			loading = false;
		}
	};

	const toggle = async () => {
		expanded = !expanded;
		if (expanded) await load();
	};

	const revertChanges = async () => {
		if (!chatId || !messageId || actionLoading) return;
		if (!confirm($i18n.t('Revert changes from this response?'))) return;
		actionLoading = true;
		error = '';
		actionMessage = '';
		try {
			const result = await revertOpenCodeMessage(localStorage.token, chatId, messageId);
			if (!result.reverted) throw new Error($i18n.t('Failed to revert changes'));
			reverted = true;
			actionMessage = $i18n.t('Changes reverted');
			await load(true);
		} catch (value) {
			error = getError(value, $i18n.t('Failed to revert changes'));
		} finally {
			actionLoading = false;
		}
	};

	const restoreChanges = async () => {
		if (!chatId || actionLoading) return;
		actionLoading = true;
		if (!confirm($i18n.t('Restore reverted changes?'))) return;
		error = '';
		actionMessage = '';
		try {
			const result = await unrevertOpenCodeChat(localStorage.token, chatId);
			if (!result.restored) throw new Error($i18n.t('Failed to restore changes'));
			reverted = false;
			actionMessage = $i18n.t('Changes restored');
			await load(true);
		} catch (value) {
			error = getError(value, $i18n.t('Failed to restore changes'));
		} finally {
			actionLoading = false;
		}
	};
</script>

<div class="mt-3 overflow-hidden rounded-md border border-gray-200 text-sm dark:border-gray-700">
	<button
		type="button"
		class="flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-gray-50 dark:hover:bg-gray-800/60"
		on:click={() => void toggle()}
		aria-expanded={expanded}
	>
		<div class="flex min-w-0 items-center gap-2">
			<CodeBracket className="size-4 shrink-0" strokeWidth="1.75" />
			<div class="min-w-0">
				<div class="truncate font-medium">{$i18n.t('Agent workspace')}</div>
				{#if currentVcs.branch}
					<div class="truncate font-mono text-[11px] text-gray-500 dark:text-gray-400">
						{currentVcs.branch}
					</div>
				{/if}
			</div>
		</div>
		<div class="flex shrink-0 items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
			<span>{$i18n.t('{{count}} changed files', { count: items?.length ?? count })}</span>
			{#if loading}
				<Spinner className="size-4" />
			{:else if expanded}
				<ChevronUp className="size-4" />
			{:else}
				<ChevronDown className="size-4" />
			{/if}
		</div>
	</button>

	{#if expanded}
		<div class="border-t border-gray-200 dark:border-gray-700">
			<div class="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-xs">
				<div class="min-w-0 text-gray-500 dark:text-gray-400">
					<span>{currentAgent || $i18n.t('Agent')}</span>
					{#if currentModel}
						<span class="mx-1">·</span>
						<span class="font-mono">{currentModel}</span>
					{/if}
				</div>
				{#if messageId}
					<button
						type="button"
						class="font-medium text-gray-600 hover:text-gray-950 disabled:opacity-50 dark:text-gray-300 dark:hover:text-white"
						disabled={actionLoading}
						on:click={() => void (reverted ? restoreChanges() : revertChanges())}
					>
						{reverted ? $i18n.t('Restore changes') : $i18n.t('Revert changes')}
					</button>
				{/if}
			</div>

			{#if error}
				<div
					class="border-t border-gray-100 px-3 py-2 text-xs text-red-600 dark:border-gray-800 dark:text-red-400"
				>
					{error}
				</div>
			{:else if actionMessage}
				<div
					class="border-t border-gray-100 px-3 py-2 text-xs text-green-700 dark:border-gray-800 dark:text-green-400"
				>
					{actionMessage}
				</div>
			{/if}

			{#if currentTodos.length > 0}
				<div class="border-t border-gray-100 px-3 py-2 dark:border-gray-800">
					<div class="mb-1.5 text-[11px] font-medium uppercase text-gray-500 dark:text-gray-400">
						{$i18n.t('Tasks')}
					</div>
					<div class="space-y-1.5">
						{#each currentTodos as todo}
							<div class="flex items-start gap-2 text-xs">
								<span
									class="mt-1.5 size-2 shrink-0 rounded-full {todo.status === 'completed'
										? 'bg-green-500'
										: todo.status === 'in_progress'
											? 'bg-blue-500'
											: 'bg-gray-300 dark:bg-gray-600'}"
								/>
								<span class:line-through={todo.status === 'completed'}>{todo.content}</span>
							</div>
						{/each}
					</div>
				</div>
			{/if}

			<div class="border-t border-gray-100 dark:border-gray-800">
				<div class="px-3 py-2 text-[11px] font-medium uppercase text-gray-500 dark:text-gray-400">
					{$i18n.t('Changed files')}
				</div>
				{#if items?.length === 0}
					<div class="px-3 pb-3 text-xs text-gray-500">{$i18n.t('No changed files')}</div>
				{:else}
					{#each items ?? [] as item}
						<div class="border-t border-gray-100 dark:border-gray-800">
							<div class="flex items-center justify-between gap-3 px-3 py-2">
								<span class="min-w-0 truncate font-mono text-xs">{getFileName(item)}</span>
								<span class="shrink-0 font-mono text-[11px]">
									<span class="text-green-600">+{getNumber(item.additions)}</span>
									<span class="ml-2 text-red-600">-{getNumber(item.deletions)}</span>
								</span>
							</div>
							{#if getPreview(item)}
								<pre
									class="max-h-72 overflow-auto border-t border-gray-100 bg-gray-50 px-3 py-2 text-[11px] leading-5 dark:border-gray-800 dark:bg-gray-900">{getPreview(
										item
									)}</pre>
							{/if}
						</div>
					{/each}
				{/if}
			</div>
		</div>
	{/if}
</div>
