<script lang="ts">
	import { getContext, onDestroy, onMount, tick } from 'svelte';
	const i18n = getContext('i18n');

	import ChatBubble from '$lib/components/icons/ChatBubble.svelte';
	import LightBulb from '$lib/components/icons/LightBulb.svelte';
	import { upsertGlossaryEntry } from '$lib/apis/glossary';
	import { toast } from 'svelte-sonner';

	export let id = '';
	export let chatId = '';
	export let messageId = '';

	export let actions = [];
	export let onSetInputText = (text) => {};

	let floatingInput = false;
	let selectedAction = null;

	let selectedText = '';
	let floatingInputValue = '';
	let correctionSource = '';
	let correctionTarget = '';
	let selectedTextIsChinese = false;
	let floatingButtonsContainer: HTMLDivElement | null = null;
	let originalParent: Node | null = null;
	let visibleActions = [];
	let canSubmitFloatingInput = false;

	const isMostlyChinese = (text: string) => {
		const compact = text.replace(/\s+/g, '');
		if (!compact) return false;
		const chinese = compact.match(/[\u3400-\u9fff]/g)?.length ?? 0;
		return chinese / compact.length >= 0.5;
	};

	const DEFAULT_ACTIONS = [
		{
			id: 'ask',
			label: $i18n.t('Ask'),
			icon: ChatBubble,
			input: true,
			prompt: `{{SELECTED_CONTENT}}\n\n\n{{INPUT_CONTENT}}`
		},
		{
			id: 'explain',
			label: $i18n.t('Explain'),
			icon: LightBulb,
			prompt: `{{SELECTED_CONTENT}}\n\n\n${$i18n.t('Explain')}`
		},
		{
			id: 'correct_translation',
			label: $i18n.t('Correct translation'),
			input: true,
			glossary: true
		}
	];

	$: visibleActions = [
		...(actions.length === 0 ? DEFAULT_ACTIONS : actions),
		...((actions.length === 0 ? DEFAULT_ACTIONS : actions).some(
			(action) => action.id === 'correct_translation'
		)
			? []
			: [DEFAULT_ACTIONS.find((action) => action.id === 'correct_translation')])
	].filter(Boolean);

	$: canSubmitFloatingInput = selectedAction?.glossary
		? selectedTextIsChinese
			? floatingInputValue.trim() !== ''
			: correctionSource.trim() !== '' && correctionTarget.trim() !== ''
		: floatingInputValue.trim() !== '';

	const resetFloatingPosition = () => {
		if (!floatingButtonsContainer) return;
		floatingButtonsContainer.style.position = 'fixed';
		floatingButtonsContainer.style.left = '';
		floatingButtonsContainer.style.right = '';
		floatingButtonsContainer.style.top = '';
		floatingButtonsContainer.style.zIndex = '2147483647';
	};

	const positionAsTopLayer = async () => {
		if (!floatingButtonsContainer) return;

		const selection = window.getSelection();
		const rect =
			selection && selection.rangeCount > 0
				? selection.getRangeAt(0).getBoundingClientRect()
				: floatingButtonsContainer.getBoundingClientRect();

		await tick();

		const panelRect = floatingButtonsContainer.getBoundingClientRect();
		const margin = 12;
		const width = panelRect.width || 420;
		const height = panelRect.height || 160;
		const left = Math.min(Math.max(rect.left, margin), window.innerWidth - width - margin);
		const belowTop = rect.bottom + 8;
		const aboveTop = rect.top - height - 8;
		const top =
			belowTop + height <= window.innerHeight - margin ? belowTop : Math.max(margin, aboveTop);

		floatingButtonsContainer.style.position = 'fixed';
		floatingButtonsContainer.style.left = `${left}px`;
		floatingButtonsContainer.style.right = 'auto';
		floatingButtonsContainer.style.top = `${top}px`;
		floatingButtonsContainer.style.zIndex = '2147483647';
	};

	const actionHandler = async (actionId) => {
		let selectedContent = selectedText
			.split('\n')
			.map((line) => `> ${line}`)
			.join('\n');

		let selectedAction = visibleActions.find((action) => action.id === actionId);
		if (!selectedAction) {
			return;
		}

		if (selectedAction.glossary) {
			const source = selectedTextIsChinese ? selectedText.trim() : correctionSource.trim();
			const target = selectedTextIsChinese ? floatingInputValue.trim() : correctionTarget.trim();
			if (!source || !target) {
				toast.error(
					selectedTextIsChinese
						? $i18n.t('Correct foreign translation is required')
						: $i18n.t('Chinese term and correct foreign translation are required')
				);
				return;
			}
			try {
				await upsertGlossaryEntry(localStorage.token, source, target, {
					chat_id: chatId,
					message_id: messageId
				});
				toast.success($i18n.t('Added to glossary'));
				closeHandler();
			} catch (error) {
				toast.error(`${error}`);
			}
			return;
		}

		let prompt = selectedAction?.prompt ?? '';

		// Handle: {{variableId|tool:id="toolId"}} pattern
		// This regex captures variableId and toolId from {{variableId|tool:id="toolId"}}
		const varToolPattern = /\{\{(.*?)\|tool:id="([^"]+)"\}\}/g;
		prompt = prompt.replace(varToolPattern, (match, variableId, toolId) => {
			return variableId; // Replace with just variableId
		});

		// legacy {{TOOL:toolId}} pattern (for backward compatibility)
		let toolIdPattern = /\{\{TOOL:([^\}]+)\}\}/g;

		// Remove all TOOL placeholders from the prompt
		prompt = prompt.replace(toolIdPattern, '');

		if (prompt.includes('{{INPUT_CONTENT}}') && floatingInput) {
			prompt = prompt.replace('{{INPUT_CONTENT}}', floatingInputValue);
			floatingInputValue = '';
		}

		prompt = prompt.replace('{{CONTENT}}', selectedText);
		prompt = prompt.replace('{{SELECTED_CONTENT}}', selectedContent);

		// Prepopulate the main chat input instead of inline streaming
		onSetInputText(prompt);
		closeHandler();
	};

	export const closeHandler = () => {
		selectedAction = null;
		selectedText = '';
		correctionSource = '';
		correctionTarget = '';
		selectedTextIsChinese = false;
		floatingInput = false;
		floatingInputValue = '';
		resetFloatingPosition();
	};

	onMount(() => {
		if (!floatingButtonsContainer) return;
		originalParent = floatingButtonsContainer.parentNode;
		document.body.appendChild(floatingButtonsContainer);
		floatingButtonsContainer.style.position = 'fixed';
		floatingButtonsContainer.style.zIndex = '2147483647';
	});

	onDestroy(() => {
		if (floatingButtonsContainer?.parentNode === document.body) {
			document.body.removeChild(floatingButtonsContainer);
		} else if (originalParent && floatingButtonsContainer) {
			originalParent.appendChild(floatingButtonsContainer);
		}
	});
</script>

<div
	id={`floating-buttons-${id}`}
	bind:this={floatingButtonsContainer}
	class="fixed rounded-lg mt-1 text-xs z-[2147483647]"
	style="display: none"
>
	{#if !floatingInput}
		<div
			class="flex flex-row shrink-0 p-0.5 bg-white dark:bg-gray-850 dark:text-gray-100 text-medium rounded-xl shadow-xl border border-gray-100 dark:border-gray-800"
		>
			{#each visibleActions as action}
				<button
					aria-label={action.label}
					class="px-1.5 py-[1px] hover:bg-gray-50 dark:hover:bg-gray-800 rounded-xl flex items-center gap-1 min-w-fit transition"
					on:click={async () => {
						selectedText = window.getSelection().toString();
						selectedTextIsChinese = isMostlyChinese(selectedText);
						correctionSource = selectedTextIsChinese ? selectedText : '';
						correctionTarget = selectedTextIsChinese ? '' : selectedText;
						selectedAction = action;

						if (action.glossary || action.prompt?.includes('{{INPUT_CONTENT}}')) {
							floatingInput = true;
							floatingInputValue = '';
							if (action.glossary && !selectedTextIsChinese) {
								floatingInputValue = '';
							}

							await tick();
							if (action.glossary) {
								await positionAsTopLayer();
							}
							setTimeout(() => {
								const input = document.getElementById('floating-message-input');
								if (input) {
									input.focus();
								}
							}, 0);
						} else {
							actionHandler(action.id);
						}
					}}
				>
					{#if action.icon}
						<svelte:component this={action.icon} className="size-3 shrink-0" />
					{/if}
					<div class="shrink-0">{action.label}</div>
				</button>
			{/each}
		</div>
	{:else}
		<div
			class="{selectedAction?.glossary
				? 'w-[min(420px,calc(100vw-2rem))] rounded-2xl p-3 items-end gap-2'
				: 'w-72 rounded-full py-1'} flex dark:text-gray-100 bg-white dark:bg-gray-850 border border-gray-100 dark:border-gray-800 shadow-xl"
		>
			{#if selectedAction?.glossary}
				<div class="flex flex-col gap-2 flex-1 min-w-0">
					<div class="text-[11px] text-gray-500 dark:text-gray-400 truncate">
						{$i18n.t('Selected text')}: {selectedText}
					</div>

					{#if !selectedTextIsChinese}
						<label class="flex flex-col gap-1">
							<span class="text-[11px] text-gray-600 dark:text-gray-300">
								{$i18n.t('中文是什么：')}
							</span>
							<input
								type="text"
								id="floating-message-input"
								class="w-full rounded-lg border border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-900/40 px-2.5 py-1.5 outline-hidden text-sm"
								placeholder={$i18n.t('Chinese term')}
								aria-label={$i18n.t('Chinese term')}
								bind:value={correctionSource}
								on:keydown={(e) => {
									if (e.key === 'Enter') {
										actionHandler(selectedAction?.id);
									}
								}}
							/>
						</label>
					{/if}

					<label class="flex flex-col gap-1">
						<span class="text-[11px] text-gray-600 dark:text-gray-300">
							{$i18n.t('新的外文是什么：')}
						</span>
						{#if selectedTextIsChinese}
							<input
								type="text"
								id="floating-message-input"
								class="w-full rounded-lg border border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-900/40 px-2.5 py-1.5 outline-hidden text-sm"
								placeholder={$i18n.t('Correct foreign translation')}
								aria-label={$i18n.t('Correct foreign translation')}
								bind:value={floatingInputValue}
								on:keydown={(e) => {
									if (e.key === 'Enter') {
										actionHandler(selectedAction?.id);
									}
								}}
							/>
						{:else}
							<input
								type="text"
								class="w-full rounded-lg border border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-900/40 px-2.5 py-1.5 outline-hidden text-sm"
								placeholder={$i18n.t('Correct foreign translation')}
								aria-label={$i18n.t('Correct foreign translation')}
								bind:value={correctionTarget}
								on:keydown={(e) => {
									if (e.key === 'Enter') {
										actionHandler(selectedAction?.id);
									}
								}}
							/>
						{/if}
					</label>
				</div>
			{:else}
				<input
					type="text"
					id="floating-message-input"
					class="ml-5 bg-transparent outline-hidden w-full flex-1 text-sm"
					placeholder={selectedAction?.glossary
						? $i18n.t('Correct foreign translation')
						: $i18n.t('Ask a question')}
					aria-label={selectedAction?.glossary
						? $i18n.t('Correct foreign translation')
						: $i18n.t('Ask a question')}
					bind:value={floatingInputValue}
					on:keydown={(e) => {
						if (e.key === 'Enter') {
							actionHandler(selectedAction?.id);
						}
					}}
				/>
			{/if}

			<div class="ml-1 mr-1">
				<button
					aria-label={$i18n.t('Submit question')}
					class="{canSubmitFloatingInput
						? 'bg-black text-white hover:bg-gray-900 dark:bg-white dark:text-black dark:hover:bg-gray-100 '
						: 'text-white bg-gray-200 dark:text-gray-900 dark:bg-gray-700 disabled'} transition rounded-full p-1.5 m-0.5 self-center"
					on:click={() => {
						actionHandler(selectedAction?.id);
					}}
				>
					<svg
						xmlns="http://www.w3.org/2000/svg"
						viewBox="0 0 16 16"
						fill="currentColor"
						class="size-4"
					>
						<path
							fill-rule="evenodd"
							d="M8 14a.75.75 0 0 1-.75-.75V4.56L4.03 7.78a.75.75 0 0 1-1.06-1.06l4.5-4.5a.75.75 0 0 1 1.06 0l4.5 4.5a.75.75 0 0 1-1.06 1.06L8.75 4.56v8.69A.75.75 0 0 1 8 14Z"
							clip-rule="evenodd"
						/>
					</svg>
				</button>
			</div>
		</div>
	{/if}
</div>
