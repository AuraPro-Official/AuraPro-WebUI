<script lang="ts">
	import { createEventDispatcher, tick } from 'svelte';

	import Check from '../icons/Check.svelte';
	import ChevronUpDown from '../icons/ChevronUpDown.svelte';

	type SearchableSelectItem = {
		value: string;
		label: string;
		searchTerms?: Array<string | null | undefined>;
		disabled?: boolean;
	};

	export let id = 'searchable-select';
	export let value = '';
	export let items: SearchableSelectItem[] = [];
	export let placeholder = '';
	export let emptyText = 'No results found';
	export let className =
		'w-full rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-850';
	export let inputClassName = 'px-3 py-2.5';
	export let disabled = false;
	export let allowCustom = false;

	const dispatch = createEventDispatcher<{ change: string }>();
	const listboxId = `${id}-options`;

	let triggerElement: HTMLDivElement | null = null;
	let inputElement: HTMLInputElement | null = null;
	let popupElement: HTMLDivElement | null = null;
	let open = false;
	let inputValue = '';
	let hasTyped = false;
	let activeIndex = -1;

	const normalize = (text: unknown): string =>
		String(text ?? '')
			.normalize('NFKD')
			.replace(/[\u0300-\u036f]/g, '')
			.toLocaleLowerCase()
			.trim();

	const compact = (text: string): string => text.replace(/[\s._/\\\-→]+/g, '');

	const getSearchText = (item: SearchableSelectItem): string => {
		const parts = [item.label, item.value, ...(item.searchTerms ?? [])].filter(Boolean).map(String);
		const visibleText = parts.join(' ');

		if (visibleText.includes('英语') || visibleText.includes('英文')) {
			parts.push('en', 'eng', 'english', 'ying', 'yingyu');
		}

		return normalize(parts.join(' '));
	};

	$: selectedItem = items.find((item) => item.value === value);
	$: selectedLabel = selectedItem?.label ?? value ?? '';
	$: if (!open) {
		inputValue = selectedLabel;
	}
	$: filteredItems =
		!hasTyped || !normalize(inputValue)
			? items
			: items.filter((item) => {
					const query = normalize(inputValue);
					const searchText = getSearchText(item);
					return searchText.includes(query) || compact(searchText).includes(compact(query));
				});

	const portal = (node: HTMLElement) => {
		document.body.appendChild(node);
		return {
			destroy() {
				node.remove();
			}
		};
	};

	const positionPopup = () => {
		if (!open || !triggerElement || !popupElement) return;

		const rect = triggerElement.getBoundingClientRect();
		const viewportPadding = 8;
		const gap = 4;
		const width = Math.min(Math.max(rect.width, 220), window.innerWidth - viewportPadding * 2);
		const desiredHeight = Math.min(popupElement.scrollHeight, 288);
		const spaceBelow = window.innerHeight - rect.bottom - viewportPadding;
		const spaceAbove = rect.top - viewportPadding;
		const showAbove = spaceBelow < desiredHeight + gap && spaceAbove > spaceBelow;

		popupElement.style.width = `${width}px`;
		popupElement.style.left = `${Math.max(
			viewportPadding,
			Math.min(rect.left, window.innerWidth - width - viewportPadding)
		)}px`;
		popupElement.style.top = showAbove
			? `${Math.max(viewportPadding, rect.top - desiredHeight - gap)}px`
			: `${rect.bottom + gap}px`;
	};

	const closeList = () => {
		open = false;
		hasTyped = false;
		activeIndex = -1;
		inputValue = selectedLabel;
	};

	const openList = async (selectText = false) => {
		if (disabled) return;

		if (!open) {
			open = true;
			hasTyped = false;
			inputValue = selectedLabel;
			activeIndex = Math.max(
				0,
				items.findIndex((item) => item.value === value && !item.disabled)
			);
		}

		await tick();
		positionPopup();
		if (selectText && inputValue) {
			inputElement?.select();
		}
	};

	const selectItem = (item: SearchableSelectItem) => {
		if (item.disabled) return;

		value = item.value;
		inputValue = item.label;
		open = false;
		hasTyped = false;
		activeIndex = -1;
		dispatch('change', value);
	};

	const commitCustomValue = () => {
		const nextValue = inputValue.trim();
		if (!allowCustom || !nextValue || (!hasTyped && nextValue === value)) return false;

		value = nextValue;
		inputValue = nextValue;
		open = false;
		hasTyped = false;
		activeIndex = -1;
		dispatch('change', value);
		return true;
	};

	const moveActive = (direction: 1 | -1) => {
		if (filteredItems.length === 0) return;

		let nextIndex = activeIndex;
		for (let count = 0; count < filteredItems.length; count += 1) {
			nextIndex = (nextIndex + direction + filteredItems.length) % filteredItems.length;
			if (!filteredItems[nextIndex]?.disabled) {
				activeIndex = nextIndex;
				break;
			}
		}
	};

	const handleInput = async () => {
		hasTyped = true;
		open = true;
		activeIndex = 0;
		await tick();
		positionPopup();
	};

	const handleKeydown = async (event: KeyboardEvent) => {
		if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
			event.preventDefault();
			await openList();
			moveActive(event.key === 'ArrowDown' ? 1 : -1);
			return;
		}

		if (event.key === 'Enter' && open) {
			event.preventDefault();
			const item = filteredItems[activeIndex];
			if (item) {
				selectItem(item);
			} else {
				commitCustomValue();
			}
			return;
		}

		if (event.key === 'Escape' && open) {
			event.preventDefault();
			closeList();
			inputElement?.blur();
		}
	};

	const handlePointerDown = (event: PointerEvent) => {
		if (!open) return;

		const target = event.target as Node;
		if (triggerElement?.contains(target) || popupElement?.contains(target)) return;
		closeList();
	};
</script>

<svelte:window
	on:pointerdown={handlePointerDown}
	on:scroll|capture={positionPopup}
	on:resize={positionPopup}
/>

<div bind:this={triggerElement} class="relative {className}">
	<input
		bind:this={inputElement}
		bind:value={inputValue}
		{id}
		type="text"
		class="block w-full bg-transparent pr-9 text-sm text-gray-900 outline-hidden placeholder:text-gray-400 disabled:cursor-not-allowed disabled:opacity-60 dark:text-gray-100 {inputClassName}"
		{placeholder}
		{disabled}
		role="combobox"
		aria-autocomplete="list"
		aria-haspopup="listbox"
		aria-controls={listboxId}
		aria-expanded={open}
		aria-activedescendant={open && activeIndex >= 0 ? `${listboxId}-${activeIndex}` : undefined}
		autocomplete="off"
		on:focus={() => openList(true)}
		on:click={() => openList(true)}
		on:input={handleInput}
		on:keydown={handleKeydown}
		on:blur={() => {
			setTimeout(() => {
				if (!popupElement?.contains(document.activeElement) && !commitCustomValue()) closeList();
			}, 0);
		}}
	/>
	<div
		class="pointer-events-none absolute inset-y-0 right-0 flex w-9 items-center justify-center text-gray-400"
	>
		<ChevronUpDown className="size-4" strokeWidth="1.8" />
	</div>
</div>

{#if open}
	<div
		use:portal
		bind:this={popupElement}
		id={listboxId}
		class="fixed z-[9999] max-h-72 overflow-y-auto rounded-lg border border-gray-200 bg-white p-1 shadow-lg dark:border-gray-700 dark:bg-gray-850"
		role="listbox"
		style="top: 0; left: 0;"
	>
		{#each filteredItems as item, index}
			<button
				id={`${listboxId}-${index}`}
				type="button"
				class="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm transition-colors {index ===
				activeIndex
					? 'bg-gray-100 dark:bg-gray-800'
					: 'hover:bg-gray-50 dark:hover:bg-gray-800/60'} disabled:cursor-not-allowed disabled:opacity-50"
				role="option"
				aria-selected={value === item.value}
				disabled={item.disabled}
				on:mouseenter={() => {
					if (!item.disabled) activeIndex = index;
				}}
				on:mousedown={(event) => event.preventDefault()}
				on:click={() => selectItem(item)}
			>
				<span class="min-w-0 flex-1 truncate">{item.label}</span>
				{#if value === item.value}
					<Check className="size-4 shrink-0" strokeWidth="2" />
				{/if}
			</button>
		{:else}
			{#if allowCustom && inputValue.trim()}
				<button
					type="button"
					class="w-full rounded-md px-3 py-2 text-left text-sm hover:bg-gray-50 dark:hover:bg-gray-800/60"
					on:mousedown={(event) => event.preventDefault()}
					on:click={commitCustomValue}
				>
					{inputValue.trim()}
				</button>
			{:else}
				<div class="px-3 py-3 text-sm text-gray-500 dark:text-gray-400">{emptyText}</div>
			{/if}
		{/each}
	</div>
{/if}
