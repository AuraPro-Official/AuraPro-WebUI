<script lang="ts">
	import { getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';

	import { getTutorialText, tutorialSections, tutorialUiText } from '$lib/tutorials';

	const i18n: Writable<i18nType> = getContext('i18n');

	let search = '';
	let activeSectionId = tutorialSections[0]?.id ?? '';

	$: language = $i18n.language ?? 'zh-CN';
	$: normalizedSearch = search.trim().toLowerCase();
	$: localizedSections = tutorialSections
		.map((section) => {
			const title = getTutorialText(section.title, language);
			const description = getTutorialText(section.description, language);
			const sectionMatches = `${title} ${description}`.toLowerCase().includes(normalizedSearch);
			const items = section.items
				.map((item) => {
					const itemTitle = getTutorialText(item.title, language);
					const summary = getTutorialText(item.summary, language);
					const steps = item.steps.map((step) => getTutorialText(step, language));
					const tips = item.tips?.map((tip) => getTutorialText(tip, language)) ?? [];
					const links =
						item.links?.map((link) => ({
							label: getTutorialText(link.label, language),
							url: link.url
						})) ?? [];
					const searchText =
						`${itemTitle} ${summary} ${steps.join(' ')} ${tips.join(' ')} ${links.map((link) => link.label).join(' ')}`.toLowerCase();

					return {
						id: item.id,
						title: itemTitle,
						summary,
						steps,
						tips,
						links,
						searchText
					};
				})
				.filter(
					(item) =>
						!normalizedSearch || sectionMatches || item.searchText.includes(normalizedSearch)
				);

			return {
				id: section.id,
				title,
				description,
				items
			};
		})
		.filter((section) => section.items.length > 0);
	$: if (
		localizedSections.length > 0 &&
		!localizedSections.some((section) => section.id === activeSectionId)
	) {
		activeSectionId = localizedSections[0].id;
	}
	$: activeSection =
		localizedSections.find((section) => section.id === activeSectionId) ?? localizedSections[0];
</script>

<div class="flex flex-col gap-4 text-sm text-gray-700 dark:text-gray-200">
	<div class="space-y-1">
		<div class="text-lg font-medium">{$i18n.t('Help')}</div>
		<div class="max-w-2xl text-xs leading-5 text-gray-500 dark:text-gray-400">
			{getTutorialText(tutorialUiText.intro, language)}
		</div>
	</div>

	<label class="block">
		<span class="sr-only">{getTutorialText(tutorialUiText.searchPlaceholder, language)}</span>
		<input
			class="w-full rounded-lg border border-gray-100 bg-gray-50 px-3 py-2 text-sm outline-none transition placeholder:text-gray-400 focus:border-gray-200 dark:border-gray-800 dark:bg-gray-900 dark:placeholder:text-gray-500 dark:focus:border-gray-700"
			bind:value={search}
			placeholder={getTutorialText(tutorialUiText.searchPlaceholder, language)}
		/>
	</label>

	{#if localizedSections.length === 0}
		<div
			class="rounded-lg border border-gray-100 bg-gray-50 p-5 text-center text-xs text-gray-500 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400"
		>
			{getTutorialText(tutorialUiText.noResults, language)}
		</div>
	{:else}
		<div class="grid gap-4 md:grid-cols-[12rem_minmax(0,1fr)]">
			<div class="space-y-1">
				{#each localizedSections as section}
					<button
						type="button"
						class={`w-full rounded-lg px-3 py-2 text-left transition ${
							activeSection?.id === section.id
								? 'bg-gray-100 text-gray-900 dark:bg-gray-800 dark:text-white'
								: 'text-gray-500 hover:bg-gray-50 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-900 dark:hover:text-white'
						}`}
						on:click={() => {
							activeSectionId = section.id;
						}}
					>
						<span class="block text-xs font-medium">{section.title}</span>
						<span class="mt-0.5 block text-[11px] leading-4 opacity-70">{section.description}</span>
					</button>
				{/each}
			</div>

			<div class="space-y-3">
				{#if activeSection}
					<div class="space-y-0.5">
						<h2 class="text-base font-semibold">{activeSection.title}</h2>
						<p class="text-xs text-gray-500 dark:text-gray-400">{activeSection.description}</p>
					</div>

					{#each activeSection.items as item}
						<article
							class="rounded-lg border border-gray-100 bg-white p-4 shadow-xs dark:border-gray-800 dark:bg-gray-950"
						>
							<div class="space-y-1">
								<h3 class="text-sm font-semibold">{item.title}</h3>
								<p class="text-xs leading-5 text-gray-500 dark:text-gray-400">{item.summary}</p>
							</div>

							<div class="mt-3 space-y-2">
								<p class="text-[11px] font-medium uppercase tracking-wide text-gray-400">
									{getTutorialText(tutorialUiText.stepsLabel, language)}
								</p>
								<ol class="space-y-1.5 pl-4 text-xs leading-5 text-gray-600 dark:text-gray-300">
									{#each item.steps as step}
										<li class="list-decimal">{step}</li>
									{/each}
								</ol>
							</div>

							{#if item.tips.length > 0}
								<div
									class="mt-3 rounded-lg border border-gray-100 bg-gray-50 px-3 py-2 dark:border-gray-800 dark:bg-gray-900"
								>
									<p class="text-[11px] font-medium text-gray-400">
										{getTutorialText(tutorialUiText.tipsLabel, language)}
									</p>
									<ul class="mt-1 space-y-1 text-xs leading-5 text-gray-600 dark:text-gray-300">
										{#each item.tips as tip}
											<li>{tip}</li>
										{/each}
									</ul>
								</div>
							{/if}

							{#if item.links.length > 0}
								<div class="mt-3 space-y-2">
									<p class="text-[11px] font-medium uppercase tracking-wide text-gray-400">
										{getTutorialText(tutorialUiText.linksLabel, language)}
									</p>
									<div class="flex flex-wrap gap-2">
										{#each item.links as link}
											<a
												class="rounded-lg border border-gray-100 bg-gray-50 px-2.5 py-1.5 text-xs text-gray-600 transition hover:border-gray-200 hover:bg-gray-100 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-300 dark:hover:border-gray-700 dark:hover:bg-gray-800"
												href={link.url}
												target="_blank"
												rel="noopener noreferrer"
											>
												{link.label}
											</a>
										{/each}
									</div>
								</div>
							{/if}
						</article>
					{/each}
				{/if}
			</div>
		</div>
	{/if}
</div>
