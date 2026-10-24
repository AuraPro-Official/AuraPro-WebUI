<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { createEventDispatcher, onMount, getContext, tick } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	const dispatch = createEventDispatcher();
	const i18n = getContext<Writable<i18nType>>('i18n');

	import Spinner from '$lib/components/common/Spinner.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Plus from '$lib/components/icons/Plus.svelte';
	import Connection from '$lib/components/chat/Settings/Tools/Connection.svelte';

	import AddToolServerModal from '$lib/components/AddToolServerModal.svelte';
	import ExternalKnowledge from './ExternalKnowledge.svelte';

	import { getToolServerConnections, setToolServerConnections } from '$lib/apis/configs';

	type ToolServerConnection = any;
	let servers: ToolServerConnection[] | null = null;
	let showConnectionModal = false;

	const addConnectionHandler = async (server: ToolServerConnection) => {
		servers = [...(servers ?? []), server];
		await updateHandler();
	};

	const updateHandler = async () => {
		const res = await setToolServerConnections(localStorage.token, {
			TOOL_SERVER_CONNECTIONS: servers
		}).catch((err) => {
			toast.error($i18n.t('Failed to save connections'));
			return null;
		});

		if (res) {
			toast.success($i18n.t('Connections saved successfully'));
		}
	};

	onMount(async () => {
		const res = await getToolServerConnections(localStorage.token);
		servers = res.TOOL_SERVER_CONNECTIONS as ToolServerConnection[];
	});
</script>

<AddToolServerModal bind:show={showConnectionModal} onSubmit={addConnectionHandler} />

<form
	class="flex flex-col h-full justify-between text-sm"
	on:submit|preventDefault={() => {
		updateHandler();
	}}
>
	<div class=" overflow-y-scroll scrollbar-hidden h-full">
		{#if servers !== null}
			<div class="">
				<div class="mb-3">
					<div class=" mt-0.5 mb-2.5 text-base font-medium">{$i18n.t('Tools')}</div>

					<hr class=" border-gray-100/30 dark:border-gray-850/30 my-2" />

					<div class="mb-2.5 flex flex-col w-full justify-between">
						<div class="flex justify-between items-center mb-0.5">
							<div class="font-medium">{$i18n.t('External Tool Servers')}</div>

							<Tooltip content={$i18n.t(`Add Connection`)}>
								<button
									class="px-1"
									on:click={() => {
										showConnectionModal = true;
									}}
									type="button"
								>
									<Plus />
								</button>
							</Tooltip>
						</div>

						<div class="flex flex-col gap-1">
							{#each servers ?? [] as server, idx}
								<Connection
									bind:connection={server}
									onSubmit={() => {
										updateHandler();
									}}
									onDelete={() => {
										servers = (servers ?? []).filter((_, i) => i !== idx);
										updateHandler();
									}}
								/>
							{/each}
						</div>

						{#if (servers ?? []).length === 0}
							<div class="text-xs text-gray-400 dark:text-gray-500">
								{$i18n.t('No tool server connections configured.')}
							</div>
						{/if}

						<div class="my-1.5">
							<div class="text-xs text-gray-500">
								{$i18n.t('Connect to your own OpenAPI compatible external tool servers.')}
							</div>
						</div>
					</div>

					<div class="mt-8 mb-2.5 text-base font-medium">{$i18n.t('Knowledge')}</div>

					<hr class=" border-gray-100/30 dark:border-gray-850/30 my-2" />

					<ExternalKnowledge />
				</div>
			</div>
		{:else}
			<div class="flex h-full justify-center">
				<div class="my-auto">
					<Spinner className="size-6" />
				</div>
			</div>
		{/if}
	</div>

	<div class="flex justify-end pt-3 text-sm font-medium">
		<button
			class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full"
			type="submit"
		>
			{$i18n.t('Save')}
		</button>
	</div>
</form>
