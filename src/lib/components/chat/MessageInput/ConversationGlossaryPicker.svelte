<script lang="ts">
	import { getContext, onMount } from 'svelte';

	import type { i18n as i18nType } from 'i18next';
	import type { Writable } from 'svelte/store';
	import SearchableSelect from '$lib/components/common/SearchableSelect.svelte';

	import {
		mergeRecentGlossaryLanguages,
		normalizeConversationGlossary,
		type ConversationGlossaryConfig,
		type GlossarySettings
	} from '$lib/utils/conversation-glossary';

	const i18n: Writable<i18nType> = getContext('i18n');
	const RECENT_LANGUAGES_STORAGE_KEY = 'aurapro-recent-glossary-languages';

	export let glossarySettings: GlossarySettings | null = null;
	export let conversationGlossary: ConversationGlossaryConfig | null = null;
	export let onChange: (value: ConversationGlossaryConfig) => void = () => {};

	let selection = 'smart';
	let recentLanguages: string[] = [];
	let hasMounted = false;
	let recordedLanguagePair = '';
	$: selection =
		conversationGlossary?.mode === 'fixed' && conversationGlossary.glossary_id
			? `fixed:${conversationGlossary.glossary_id}`
			: 'smart';
	$: languageItems = recentLanguages.map((language) => ({ value: language, label: language }));
	$: activeLanguages =
		conversationGlossary?.mode === 'smart'
			? [conversationGlossary.source_lang, conversationGlossary.target_lang].filter(
					(language): language is string => Boolean(language)
				)
			: [];
	$: activeLanguagePair = activeLanguages.join('|');
	$: if (hasMounted && activeLanguagePair !== recordedLanguagePair) {
		recordedLanguagePair = activeLanguagePair;
		rememberLanguages(activeLanguages);
	}

	const rememberLanguages = (languages: unknown[]) => {
		recentLanguages = mergeRecentGlossaryLanguages(recentLanguages, languages);
		try {
			localStorage.setItem(RECENT_LANGUAGES_STORAGE_KEY, JSON.stringify(recentLanguages));
		} catch (error) {
			console.debug('Unable to save recent glossary languages', error);
		}
	};

	onMount(() => {
		try {
			recentLanguages = mergeRecentGlossaryLanguages(
				[],
				JSON.parse(localStorage.getItem(RECENT_LANGUAGES_STORAGE_KEY) ?? '[]')
			);
		} catch (error) {
			console.debug('Unable to load recent glossary languages', error);
		}
		hasMounted = true;
	});

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

	const changeSmartLanguage = (field: 'source_lang' | 'target_lang', language: string) => {
		emit({
			mode: 'smart',
			source_lang: conversationGlossary?.source_lang,
			target_lang: conversationGlossary?.target_lang,
			[field]: language
		});
		rememberLanguages([language]);
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
					<SearchableSelect
						id="conversation-glossary-source-language"
						className="w-full rounded-md border border-gray-200 bg-transparent dark:border-gray-700"
						inputClassName="px-2 py-1.5"
						value={conversationGlossary.source_lang ?? ''}
						items={languageItems}
						allowCustom={true}
						placeholder={$i18n.t('Enter or select a language')}
						emptyText={$i18n.t('Enter or select a language')}
						on:change={(event) => changeSmartLanguage('source_lang', event.detail)}
					/>
				</label>
				<label class="min-w-0 text-[11px] text-gray-500 dark:text-gray-400">
					<span class="mb-1 block">{$i18n.t('Target language')}</span>
					<SearchableSelect
						id="conversation-glossary-target-language"
						className="w-full rounded-md border border-gray-200 bg-transparent dark:border-gray-700"
						inputClassName="px-2 py-1.5"
						value={conversationGlossary.target_lang ?? ''}
						items={languageItems}
						allowCustom={true}
						placeholder={$i18n.t('Enter or select a language')}
						emptyText={$i18n.t('Enter or select a language')}
						on:change={(event) => changeSmartLanguage('target_lang', event.detail)}
					/>
				</label>
			</div>
		{/if}

		<p class="mt-1.5 text-[11px] leading-4 text-gray-400 dark:text-gray-500">
			{$i18n.t('This selection only applies to the current conversation.')}
		</p>
	</div>
{/if}
