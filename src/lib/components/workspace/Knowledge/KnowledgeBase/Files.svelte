<script lang="ts">
	import dayjs from '$lib/dayjs';
	import duration from 'dayjs/plugin/duration';
	import relativeTime from 'dayjs/plugin/relativeTime';

	dayjs.extend(duration);
	dayjs.extend(relativeTime);

	import { getContext } from 'svelte';
	const i18n = getContext('i18n');

	import { capitalizeFirstLetter, formatFileSize } from '$lib/utils';

	import { WEBUI_BASE_URL } from '$lib/constants';

	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import Dropdown from '$lib/components/common/Dropdown.svelte';
	import DocumentPage from '$lib/components/icons/DocumentPage.svelte';
	import EllipsisHorizontal from '$lib/components/icons/EllipsisHorizontal.svelte';
	import Download from '$lib/components/icons/Download.svelte';
	import GarbageBin from '$lib/components/icons/GarbageBin.svelte';
	import Pencil from '$lib/components/icons/Pencil.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import DirectoryRow from './DirectoryRow.svelte';

	export let knowledge = null;
	export let selectedFileId = null;
	export let files = [];
	export let directories = [];

	export let onClick = (fileId) => {};
	export let onDelete = (fileId) => {};
	export let onRename = (fileId: string, name: string) => {};
	export let onNavigateDirectory = (directoryId: string) => {};
	export let onRenameDirectory = (id: string, name: string) => {};
	export let onDeleteDirectory = (id: string) => {};
	export let onMoveFileToDirectory = (fileId: string, directoryId: string) => {};
	export let onMoveDirectoryToDirectory = (dirId: string, targetDirectoryId: string) => {};

	let editingFileId: string | null = null;
	let editName = '';
	let editInput: HTMLInputElement;

	const progressMessageKeys: Record<string, string> = {
		preparing: 'Preparing the file...',
		reading_content: 'Reading file content...',
		extracting_content: 'Extracting text from the file...',
		content_extracted: 'Text extraction complete; preparing chunks...',
		linking_to_knowledge: 'Adding the file to the knowledge base...',
		preparing_vectors: 'Preparing knowledge base vectors...',
		splitting_documents: 'Splitting content into searchable chunks...',
		search_embeddings: 'Generating semantic search vectors...',
		saving_vectors: 'Writing vectors to the knowledge base...',
		finalizing: 'Finalizing the knowledge base...',
		complete: 'Processing complete'
	};

	const getProgressLabel = (file: any) =>
		$i18n.t(progressMessageKeys[file?.progressStage] ?? 'Processing knowledge base...');

	const getElapsedLabel = (file: any) => {
		if (!file?.progressStartedAt) return '';
		const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - file.progressStartedAt));
		if (elapsed < 60) return $i18n.t('{{count}} sec', { count: elapsed });
		return $i18n.t('{{count}} min', { count: Math.floor(elapsed / 60) });
	};

	const startRename = (file: any) => {
		editingFileId = file?.id ?? file?.tempId;
		editName = file?.name ?? file?.meta?.name ?? '';
		setTimeout(() => editInput?.select(), 0);
	};

	const submitRename = () => {
		if (editingFileId && editName.trim()) {
			onRename(editingFileId, editName.trim());
		}
		editingFileId = null;
	};

	const cancelRename = () => {
		editingFileId = null;
	};
</script>

<div class=" max-h-full flex flex-col w-full gap-[0.5px]">
	<!-- Directories first -->
	{#each directories as dir (dir.id)}
		<DirectoryRow
			directory={dir}
			writeAccess={knowledge?.write_access}
			onNavigate={(id) => onNavigateDirectory(id)}
			onRename={(id, name) => onRenameDirectory(id, name)}
			onDelete={(id) => onDeleteDirectory(id)}
			onFileDrop={(fileId, directoryId) => onMoveFileToDirectory(fileId, directoryId)}
			onDirDrop={(dirId, targetId) => onMoveDirectoryToDirectory(dirId, targetId)}
		/>
	{/each}

	<!-- Files -->
	{#each files as file (file?.id ?? file?.itemId ?? file?.tempId)}
		<div
			class=" flex cursor-pointer w-full px-2 bg-transparent dark:hover:bg-gray-850/50 hover:bg-white rounded-xl transition {selectedFileId
				? ''
				: 'hover:bg-gray-100 dark:hover:bg-gray-850'}"
			draggable="true"
			on:dragstart={(e) => {
				const fileId = file?.id ?? file?.tempId;
				if (fileId) {
					e.dataTransfer?.setData('application/x-kb-file-move', JSON.stringify({ fileId }));
				}
			}}
		>
			<div class="flex items-center">
				{#if file?.status !== 'uploading'}
					<button
						class="p-1 rounded-full transition"
						type="button"
						on:click={() => {
							let fileId = file?.id ?? file?.tempId;
							onClick(fileId);
						}}
					>
						<DocumentPage className="size-3.5" />
					</button>
				{:else}
					<Spinner className="size-3.5" />
				{/if}
			</div>

			<button
				class="relative flex items-center gap-1 rounded-xl p-2 text-left flex-1 justify-between"
				type="button"
				on:click={() => {
					if (editingFileId) return;
					onClick(file?.id ?? file?.tempId);
				}}
				on:dblclick={() => {
					if (knowledge?.write_access) startRename(file);
				}}
			>
				<div class="min-w-0 flex-1">
					<div class="flex gap-2 items-center line-clamp-1">
						{#if editingFileId === (file?.id ?? file?.tempId)}
							<!-- svelte-ignore a11y-autofocus -->
							<input
								bind:this={editInput}
								bind:value={editName}
								class="text-sm w-full bg-transparent border-none outline-hidden"
								on:keydown={(e) => {
									if (e.key === 'Enter') submitRename();
									if (e.key === 'Escape') cancelRename();
									if (e.key === ' ') e.stopPropagation();
								}}
								on:keyup={(e) => {
									if (e.key === ' ') e.stopPropagation();
								}}
								on:blur={submitRename}
								on:click={(e) => e.stopPropagation()}
								autofocus
							/>
						{:else}
							<div class="line-clamp-1 text-sm">
								{file?.name ?? file?.meta?.name}
								{#if file?.meta?.size}
									<span class="text-xs text-gray-500">{formatFileSize(file?.meta?.size)}</span>
								{/if}
							</div>
						{/if}
					</div>
					{#if file?.status === 'uploading'}
						<div class="mt-1 min-w-[14rem] max-w-md">
							<div
								class="flex items-center justify-between gap-3 text-[11px] text-gray-500 dark:text-gray-400"
							>
								<span class="truncate">{getProgressLabel(file)}</span>
								<span class="shrink-0">
									{file?.progress ?? 0}%
									{#if getElapsedLabel(file)}
										· {$i18n.t('Elapsed {{time}}', { time: getElapsedLabel(file) })}
									{/if}
								</span>
							</div>
							<div class="mt-1 h-1 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
								<div
									class="h-1 rounded-full bg-blue-500 transition-[width] duration-300"
									style="width: {file?.progress ?? 0}%"
								/>
							</div>
							{#if file?.progressColdStart}
								<div class="mt-1 text-[11px] text-amber-600 dark:text-amber-400">
									{$i18n.t('First use usually takes 5–20 minutes')}
								</div>
							{/if}
						</div>
					{/if}
				</div>

				<div class="flex items-center gap-2 shrink-0">
					{#if file?.updated_at}
						<Tooltip content={dayjs(file.updated_at * 1000).format('LLLL')}>
							<div class="text-xs text-gray-400">
								{dayjs(file.updated_at * 1000).fromNow()}
							</div>
						</Tooltip>
					{/if}

					{#if file?.user}
						<Tooltip
							content={file?.user?.email ?? $i18n.t('Deleted User')}
							className="flex shrink-0"
							placement="top-start"
						>
							<div class="shrink-0 text-gray-500">
								{$i18n.t('By {{name}}', {
									name: capitalizeFirstLetter(
										file?.user?.name ?? file?.user?.email ?? $i18n.t('Deleted User')
									)
								})}
							</div>
						</Tooltip>
					{/if}
				</div>
			</button>

			{#if knowledge?.write_access}
				<div class="flex items-center">
					<Dropdown align="end" sideOffset={4}>
						<button
							class="p-1 rounded-full hover:bg-gray-100 dark:hover:bg-gray-850 transition"
							type="button"
						>
							<EllipsisHorizontal className="size-3.5" />
						</button>

						<div slot="content">
							<div
								class="min-w-[140px] rounded-2xl p-1 z-[9999999] bg-white dark:bg-gray-850 dark:text-white shadow-lg border border-gray-100 dark:border-gray-800"
							>
								<button
									type="button"
									class="select-none flex rounded-xl py-1.5 px-3 w-full hover:bg-gray-50 dark:hover:bg-gray-800 transition items-center gap-2 text-sm"
									on:click={() => {
										startRename(file);
									}}
								>
									<Pencil className="size-3.5" />
									{$i18n.t('Rename')}
								</button>
								<button
									type="button"
									class="select-none flex rounded-xl py-1.5 px-3 w-full hover:bg-gray-50 dark:hover:bg-gray-800 transition items-center gap-2 text-sm"
									on:click={() => {
										let fileId = file?.id ?? file?.tempId;
										window.open(
											`${WEBUI_BASE_URL}/api/v1/files/${fileId}/content`,
											'_blank',
											'noopener,noreferrer'
										);
									}}
								>
									<Download className="size-3.5" />
									{$i18n.t('Download')}
								</button>
								<button
									type="button"
									class="select-none flex rounded-xl py-1.5 px-3 w-full hover:bg-gray-50 dark:hover:bg-gray-800 transition items-center gap-2 text-sm"
									on:click={() => {
										onDelete(file?.id ?? file?.tempId);
									}}
								>
									<GarbageBin className="size-3.5" />
									{$i18n.t('Delete')}
								</button>
							</div>
						</div>
					</Dropdown>
				</div>
			{/if}
		</div>
	{/each}
</div>
