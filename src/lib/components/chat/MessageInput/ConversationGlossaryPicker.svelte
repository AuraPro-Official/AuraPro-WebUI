<script lang="ts">
	import { getContext } from 'svelte';

	import type { i18n as i18nType } from 'i18next';
	import type { Writable } from 'svelte/store';

	import {
		getGlossaryLanguages,
		normalizeConversationGlossary,
		type ConversationGlossaryConfig,
		type GlossarySettings
	} from '$lib/utils/conversation-glossary';

	const i18n: Writable<i18nType> = getContext('i18n');

	export let glossarySettings: GlossarySettings | null = null;
	export let conversationGlossary: ConversationGlossaryConfig | null = null;
	export let onChange: (value: ConversationGlossaryConfig) => void = () => {};

	let selection = 'smart';
	$: selection =
		conversationGlossary?.mode === 'fixed' && conversationGlossary.glossary_id
			? `fixed:${conversationGlossary.glossary_id}`
			: 'smart';
	$: languages = getGlossaryLanguages(glossarySettings);

	const emit = (value: unknown) => {
		const normalized = normalizeConversationGlossary(value, glossarySettings);
		if (normalized) onChange(normalized);
	};

	const changeSelection = () => {
		if (selection === 'smart') {
			emit({
				mode: 'smart',
				source_lang: conversationGlossary?.source_lang,
				target_lang: conversationGlossary?.target_lang
			});
			return;
		}

		emit({ mode: 'fixed', glossary_id: selection.slice('fixed:'.length) });
	};

	const changeSmartLanguage = (field: 'source_lang' | 'target_lang', event: Event) => {
		const input = event.currentTarget as HTMLInputElement;
		emit({
			mode: 'smart',
			source_lang: conversationGlossary?.source_lang,
			target_lang: conversationGlossary?.target_lang,
			[field]: input.value
		});
	};
</script>

{#if glossarySettings && conversationGlossary}
	<div class="mx-2 mt-1 border-t border-gray-100 px-1 pb-1 pt-2 dark:border-gray-800">
		<label
			for="conversation-glossary-selection"
			class="mb-1 block text-xs font-medium text-gray-600 dark:text-gray-300"
		>
			{$i18n.t('Current conversation glossary')}
		</label>
		<select
			id="conversation-glossary-selection"
			class="w-full rounded-md border border-gray-200 bg-transparent px-2 py-1.5 text-sm outline-hidden focus:border-gray-400 dark:border-gray-700 dark:focus:border-gray-500"
			bind:value={selection}
			on:change={changeSelection}
		>
			<option value="smart">{$i18n.t('Smart glossary')}</option>
			{#each glossarySettings.glossaries ?? [] as glossary (glossary.id)}
				<option value={`fixed:${glossary.id}`}>{glossary.name}</option>
			{/each}
		</select>

		{#if conversationGlossary.mode === 'smart'}
			<div class="mt-2 grid grid-cols-2 gap-2">
				<label class="min-w-0 text-[11px] text-gray-500 dark:text-gray-400">
					<span class="mb-1 block">{$i18n.t('Source language')}</span>
					<input
						class="w-full rounded-md border border-gray-200 bg-transparent px-2 py-1.5 text-sm text-gray-700 outline-hidden focus:border-gray-400 dark:border-gray-700 dark:text-gray-200 dark:focus:border-gray-500"
						list="conversation-glossary-languages"
						value={conversationGlossary.source_lang ?? ''}
						on:change={(event) => changeSmartLanguage('source_lang', event)}
					/>
				</label>
				<label class="min-w-0 text-[11px] text-gray-500 dark:text-gray-400">
					<span class="mb-1 block">{$i18n.t('Target language')}</span>
					<input
						class="w-full rounded-md border border-gray-200 bg-transparent px-2 py-1.5 text-sm text-gray-700 outline-hidden focus:border-gray-400 dark:border-gray-700 dark:text-gray-200 dark:focus:border-gray-500"
						list="conversation-glossary-languages"
						value={conversationGlossary.target_lang ?? ''}
						on:change={(event) => changeSmartLanguage('target_lang', event)}
					/>
				</label>
			</div>
			<datalist id="conversation-glossary-languages">
				{#each languages as language}
					<option value={language}></option>
				{/each}
			</datalist>
		{/if}

		<p class="mt-1.5 text-[11px] leading-4 text-gray-400 dark:text-gray-500">
			{$i18n.t('This selection only applies to the current conversation.')}
		</p>
	</div>
{/if}
