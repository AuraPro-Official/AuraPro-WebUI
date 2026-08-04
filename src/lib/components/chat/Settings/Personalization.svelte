<script lang="ts">
	import Switch from '$lib/components/common/Switch.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { deleteChatHistoryMemory, getMemoryStatus } from '$lib/apis/memories';
	import { config, settings } from '$lib/stores';
	import { createEventDispatcher, getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import ManageModal from './Personalization/ManageModal.svelte';

	const dispatch = createEventDispatcher();
	const i18n = getContext('i18n');

	export let saveSettings: Function;

	let showManageModal = false;
	let enableMemory = false;
	let enableChatHistoryMemory = true;
	let memoryStatus: {
		saved_count?: number;
		chat_history_summary?: string | null;
		review?: { state?: string; reason?: string };
	} | null = null;

	const loadStatus = async () => {
		memoryStatus = await getMemoryStatus(localStorage.token).catch(() => null);
	};

	const updateSavedMemory = async () => {
		if (!enableMemory) {
			enableChatHistoryMemory = false;
			await saveSettings({ memory: false, chatHistoryMemory: false });
			await deleteChatHistoryMemory(localStorage.token).catch((error) => {
				toast.error(`${error}`);
			});
			await loadStatus();
			return;
		}
		await saveSettings({ memory: true, chatHistoryMemory: enableChatHistoryMemory });
	};

	const updateChatHistoryMemory = async () => {
		await saveSettings({
			memory: enableMemory,
			chatHistoryMemory: enableMemory && enableChatHistoryMemory
		});
		if (!enableChatHistoryMemory) {
			await deleteChatHistoryMemory(localStorage.token).catch((error) => {
				toast.error(`${error}`);
			});
			await loadStatus();
		}
	};

	onMount(async () => {
		enableMemory = $settings?.memory ?? $config?.features?.enable_memories ?? false;
		enableChatHistoryMemory = enableMemory && ($settings?.chatHistoryMemory ?? true);
		await loadStatus();
	});
</script>

<ManageModal bind:show={showManageModal} />

<form
	id="tab-personalization"
	class="flex flex-col h-full justify-between space-y-3 text-sm"
	on:submit|preventDefault={() => {
		dispatch('save');
	}}
>
	<div class="py-1 overflow-y-scroll max-h-[28rem] md:max-h-full">
		<div class="flex items-center justify-between mb-1">
			<Tooltip
				content={$i18n.t(
					'Memory helps personalize future conversations and remains under your control.'
				)}
			>
				<div class="text-sm font-medium">{$i18n.t('Memory')}</div>
			</Tooltip>
		</div>

		<div class="mt-3 space-y-4">
			<div class="flex items-start justify-between gap-4">
				<div class="min-w-0">
					<div class="text-sm font-medium">{$i18n.t('Reference saved memories')}</div>
					<div class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
						{$i18n.t(
							'Use saved preferences and details in future conversations, and allow automatic memory updates.'
						)}
					</div>
				</div>
				<Switch bind:state={enableMemory} on:change={updateSavedMemory} />
			</div>

			<div class="flex items-start justify-between gap-4">
				<div class="min-w-0">
					<div class="text-sm font-medium" class:text-gray-400={!enableMemory}>
						{$i18n.t('Reference chat history')}
					</div>
					<div class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
						{$i18n.t(
							'Use a private, continuously updated summary of previous chats to improve future responses.'
						)}
					</div>
					{#if enableMemory && !enableChatHistoryMemory}
						<div class="mt-1 text-xs text-gray-400">
							{$i18n.t('Saved memories remain available when chat history is off.')}
						</div>
					{/if}
				</div>
				<div class:opacity-40={!enableMemory} class:pointer-events-none={!enableMemory}>
					<Switch
						state={enableMemory && enableChatHistoryMemory}
						on:change={async (event) => {
							enableChatHistoryMemory = event.detail;
							await updateChatHistoryMemory();
						}}
					/>
				</div>
			</div>
		</div>

		<div class="mt-4 flex items-center gap-3">
			<button
				type="button"
				class="px-3.5 py-1.5 font-medium hover:bg-black/5 dark:hover:bg-white/5 outline outline-1 outline-gray-300 dark:outline-gray-800 rounded-3xl"
				on:click={() => {
					showManageModal = true;
				}}
			>
				{$i18n.t('Manage memories')}
			</button>
			{#if memoryStatus}
				<div class="text-xs text-gray-500 dark:text-gray-400">
					{$i18n.t('{{count}} saved memories', { count: memoryStatus.saved_count ?? 0 })}
					{#if memoryStatus.chat_history_summary}
						<span class="ml-1">{$i18n.t('Chat history summary ready')}</span>
					{/if}
				</div>
			{/if}
		</div>

		{#if memoryStatus?.review?.state === 'error'}
			<div class="mt-2 text-xs text-red-600 dark:text-red-400">
				{$i18n.t('Last automatic memory review failed')}
			</div>
		{:else if memoryStatus?.review?.state === 'completed'}
			<div class="mt-2 text-xs text-emerald-600 dark:text-emerald-400">
				{$i18n.t('Automatic memory is working')}
			</div>
		{:else if memoryStatus?.review?.reason === 'automatic_review_disabled'}
			<div class="mt-2 text-xs text-gray-500 dark:text-gray-400">
				{$i18n.t('Automatic memory review is disabled by the administrator')}
			</div>
		{/if}
	</div>

	<div class="flex justify-end text-sm font-medium">
		<button
			class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full"
			type="submit"
		>
			{$i18n.t('Save')}
		</button>
	</div>
</form>
