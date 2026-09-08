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

<div class="help-page">
	<label class="help-search">
		<span class="sr-only">{getTutorialText(tutorialUiText.searchPlaceholder, language)}</span>
		<input
			bind:value={search}
			placeholder={getTutorialText(tutorialUiText.searchPlaceholder, language)}
		/>
	</label>

	<div class="help-categories" role="group" aria-label={$i18n.t('Help')}>
		{#each tutorialSections as section}
			<button
				type="button"
				class:active={activeSectionId === section.id}
				aria-pressed={activeSectionId === section.id}
				on:click={() => {
					search = '';
					activeSectionId = section.id;
				}}
			>
				{getTutorialText(section.title, language)}
			</button>
		{/each}
	</div>

	{#if localizedSections.length === 0}
		<p class="help-empty" role="status">{getTutorialText(tutorialUiText.noResults, language)}</p>
	{:else}
		{#each localizedSections.filter((section) => normalizedSearch || section.id === activeSection?.id) as section (section.id)}
			<section class="help-section">
				{#if normalizedSearch}
					<h2>{section.title}</h2>
				{/if}
				<p class="help-description">{section.description}</p>
				<div class="help-articles">
					{#each section.items as item (item.id)}
						<details class="help-article" open={Boolean(normalizedSearch)}>
							<summary>
								<span class="help-item-title">{item.title}</span>
								<span class="help-summary">{item.summary}</span>
							</summary>
							<div class="help-body">
								<ol>
									{#each item.steps as step}
										<li>{step}</li>
									{/each}
								</ol>
								{#if item.tips.length > 0}
									<aside class="help-tips">
										<p>{getTutorialText(tutorialUiText.tipsLabel, language)}</p>
										<ul>
											{#each item.tips as tip}<li>{tip}</li>{/each}
										</ul>
									</aside>
								{/if}
								{#if item.links.length > 0}
									<div class="help-links">
										<p>{getTutorialText(tutorialUiText.linksLabel, language)}</p>
										<div>
											{#each item.links as link}
												<a href={link.url} target="_blank" rel="noopener noreferrer">{link.label}</a
												>
											{/each}
										</div>
									</div>
								{/if}
							</div>
						</details>
					{/each}
				</div>
			</section>
		{/each}
	{/if}
</div>

<style>
	.help-page {
		--help-border: rgba(0, 0, 0, 0.1);
		--help-muted: #666;
		--help-accent: #08765a;
		width: 100%;
		min-width: 0;
		padding-bottom: 24px;
		color: #242424;
		font-size: 13px;
		line-height: 1.6;
		letter-spacing: 0;
	}
	:global(.dark) .help-page {
		--help-border: rgba(255, 255, 255, 0.14);
		--help-muted: #b3b3b3;
		--help-accent: #6ee7b7;
		color: #ededed;
	}
	.help-search {
		display: block;
		margin-bottom: 16px;
	}
	.help-search input {
		box-sizing: border-box;
		width: 100%;
		min-width: 0;
		padding: 9px 12px;
		border: 1px solid var(--help-border);
		border-radius: 6px;
		background: transparent;
		color: inherit;
		font: inherit;
	}
	.help-search input::placeholder {
		color: var(--help-muted);
	}
	.help-search input:focus-visible,
	.help-categories button:focus-visible,
	summary:focus-visible {
		outline: 2px solid var(--help-accent);
		outline-offset: 2px;
	}
	.help-categories {
		display: flex;
		flex-wrap: wrap;
		gap: 4px 16px;
		border-bottom: 1px solid var(--help-border);
	}
	.help-categories button {
		padding: 8px 0;
		border: 0;
		border-bottom: 2px solid transparent;
		background: transparent;
		color: var(--help-muted);
		font: inherit;
		cursor: pointer;
	}
	.help-categories button.active {
		border-bottom-color: var(--help-accent);
		color: var(--help-accent);
	}
	.help-section {
		min-width: 0;
		margin-top: 16px;
	}
	h2 {
		margin: 0;
		font-size: 14px;
		font-weight: 600;
	}
	.help-description {
		margin: 0 0 12px;
		color: var(--help-muted);
		font-size: 12px;
	}
	.help-article {
		border-bottom: 1px solid var(--help-border);
	}
	summary {
		padding: 14px 4px;
		cursor: pointer;
		overflow-wrap: anywhere;
	}
	summary::marker {
		color: var(--help-muted);
	}
	.help-item-title {
		font-weight: 600;
	}
	.help-summary {
		display: block;
		padding-left: 16px;
		margin-top: 3px;
		font-size: 12px;
		color: var(--help-muted);
	}
	.help-body {
		padding: 0 4px 20px 20px;
		overflow-wrap: anywhere;
	}
	ol {
		list-style: decimal;
		padding-left: 20px;
		margin: 0;
	}
	li + li {
		margin-top: 8px;
	}
	.help-tips {
		margin-top: 16px;
		padding-left: 12px;
		border-left: 2px solid var(--help-border);
	}
	.help-tips p,
	.help-links p {
		color: var(--help-muted);
		margin: 0 0 6px;
		font-size: 12px;
	}
	.help-tips ul {
		list-style: disc;
		padding-left: 16px;
		margin: 0;
	}
	.help-links {
		margin-top: 16px;
	}
	.help-links div {
		display: flex;
		flex-wrap: wrap;
		gap: 8px 16px;
	}
	.help-links :is(a, button) {
		padding: 0;
		border: 0;
		background: transparent;
		color: var(--help-accent);
		text-align: left;
		font: inherit;
		text-decoration: underline;
		text-underline-offset: 3px;
		cursor: pointer;
	}
	.help-empty {
		padding: 24px 0;
		text-align: center;
		color: var(--help-muted);
	}
</style>
