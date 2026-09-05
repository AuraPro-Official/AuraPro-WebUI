<script lang="ts">
	import { getContext } from 'svelte';

	import { models, settings, type Model } from '$lib/stores';
	import {
		resolveContextUsage,
		type ContextUsageDetails,
		type ContextUsageSnapshot
	} from '$lib/utils/context-usage';

	import Tooltip from '../../common/Tooltip.svelte';

	const i18n = getContext<{
		t: (key: string, variables?: Record<string, string | number>) => string;
	}>('i18n');

	export let history: {
		currentId?: string | null;
		messages?: Record<
			string,
			{
				role?: string;
				parentId?: string | null;
				contextUsage?: ContextUsageSnapshot;
				usage?: Record<string, unknown>;
			}
		>;
	};
	export let selectedModelIds: string[] = [];
	export let atSelectedModel: Model | undefined = undefined;
	export let chatParams: Record<string, unknown> = {};
	export let compacting = false;

	const colors: Record<ContextUsageDetails['level'], string> = {
		unknown: '#9ca3af',
		normal: '#10a37f',
		warning: '#d97706',
		high: '#ea580c',
		critical: '#dc2626'
	};

	const numberFormatter = new Intl.NumberFormat();
	const formatTokens = (value: number | null) =>
		value === null ? i18n.t('Unknown') : numberFormatter.format(Math.round(value));
	const escapeHtml = (value: unknown) =>
		String(value)
			.replaceAll('&', '&amp;')
			.replaceAll('<', '&lt;')
			.replaceAll('>', '&gt;')
			.replaceAll('"', '&quot;')
			.replaceAll("'", '&#039;');

	$: activeModelId = atSelectedModel?.id ?? selectedModelIds.find(Boolean) ?? '';
	$: activeModel =
		atSelectedModel ?? ($models ?? []).find((model) => model.id === activeModelId) ?? null;
	$: requestParams = {
		...($settings?.params ?? {}),
		...chatParams
	};
	$: details = resolveContextUsage({
		history,
		model: activeModel,
		requestParams
	});
	$: displayedPercent =
		details.available && details.percent !== null ? Math.max(0, Math.round(details.percent)) : null;
	$: ringProgress =
		details.available && details.percent !== null ? Math.min(100, Math.max(0, details.percent)) : 0;
	$: ringColor = compacting ? '#0ea5e9' : colors[details.level];
	$: compactionStatus = compacting
		? i18n.t('Compacting context')
		: details.hardTruncation
			? i18n.t('Hard context truncation is active')
			: !details.compactionEnabled
				? i18n.t('Disabled')
				: details.compacted
					? i18n.t('Context was compacted for the latest request')
					: i18n.t('Enabled');
	$: sourceLabel = details.available
		? details.estimated
			? i18n.t('Estimated token count')
			: i18n.t('Exact model usage')
		: i18n.t('Waiting for model usage');
	$: tooltipContent = `
		<div style="min-width: 238px; text-align: left; line-height: 1.55;">
			<div style="display: flex; justify-content: space-between; gap: 20px; margin-bottom: 5px;">
				<strong>${escapeHtml(i18n.t('Context usage'))}</strong>
				<strong>${displayedPercent === null ? '--' : `${displayedPercent}%`}</strong>
			</div>
			<div>${escapeHtml(i18n.t('Current context'))}: ${escapeHtml(formatTokens(details.usedTokens))} / ${escapeHtml(formatTokens(details.limitTokens))} tokens</div>
			<div>${escapeHtml(i18n.t('Input tokens'))}: ${escapeHtml(formatTokens(details.inputTokens))}</div>
			<div>${escapeHtml(i18n.t('Output tokens'))}: ${escapeHtml(formatTokens(details.outputTokens))}</div>
			<div>${escapeHtml(i18n.t('Compaction threshold'))}: ${escapeHtml(formatTokens(details.thresholdTokens))}${
				details.thresholdPercent === null ? '' : ` (${Math.round(details.thresholdPercent)}%)`
			}</div>
			<div>${escapeHtml(i18n.t('Automatic context compaction'))}: ${escapeHtml(compactionStatus)}</div>
			<div style="margin-top: 5px; opacity: 0.72;">${escapeHtml(sourceLabel)}${
				details.limitEstimated ? ` · ${escapeHtml(i18n.t('Context limit estimated'))}` : ''
			}</div>
		</div>
	`;
	$: ariaLabel =
		displayedPercent === null
			? i18n.t('Context usage is unavailable')
			: i18n.t('Context usage: {{percent}}%', { percent: displayedPercent });
</script>

{#if activeModelId}
	<Tooltip
		content={tooltipContent}
		placement="top"
		tippyOptions={{ maxWidth: 330 }}
		className="flex items-center"
	>
		<button
			type="button"
			class:compacting
			class="context-usage-ring"
			style="--ring-color: {ringColor};"
			aria-label={ariaLabel}
		>
			<svg viewBox="0 0 32 32" aria-hidden="true">
				<circle class="ring-track" cx="16" cy="16" r="12" pathLength="100" />
				<circle
					class="ring-progress"
					cx="16"
					cy="16"
					r="12"
					pathLength="100"
					stroke-dasharray="{ringProgress} 100"
				/>
			</svg>
			<span
				>{compacting ? '…' : (displayedPercent ?? '--')}{displayedPercent === null ? '' : '%'}</span
			>
		</button>
	</Tooltip>
{/if}

<style>
	.context-usage-ring {
		position: relative;
		width: 2rem;
		height: 2rem;
		flex: none;
		color: var(--ring-color);
		padding: 0;
		border: 0;
		background: transparent;
		outline: none;
	}

	.context-usage-ring:focus-visible {
		border-radius: 9999px;
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--ring-color) 35%, transparent);
	}

	svg {
		width: 100%;
		height: 100%;
		transform: rotate(-90deg);
	}

	circle {
		fill: none;
		stroke-width: 2.4;
	}

	.ring-track {
		stroke: currentColor;
		opacity: 0.16;
	}

	.ring-progress {
		stroke: currentColor;
		stroke-linecap: round;
		transition:
			stroke-dasharray 240ms ease,
			stroke 180ms ease;
	}

	span {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		color: currentColor;
		font-size: 0.46rem;
		font-weight: 650;
		line-height: 1;
		letter-spacing: 0;
	}

	.compacting svg {
		animation: context-ring-spin 1s linear infinite;
	}

	.compacting .ring-progress {
		stroke-dasharray: 24 76;
	}

	@keyframes context-ring-spin {
		to {
			transform: rotate(270deg);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.compacting svg {
			animation: none;
		}
	}
</style>
