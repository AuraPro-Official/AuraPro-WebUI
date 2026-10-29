<script lang="ts">
	import { getContext, onDestroy, onMount } from 'svelte';
	import {
		controlAgent,
		getAgentActivity,
		abortOpenCodeChat,
		type AgentActivity,
		type AgentInteraction
	} from '$lib/apis/opencode';
	import { piToolLabel, piStatusLabel } from '$lib/utils/pi-language';
	import ChevronDown from '$lib/components/icons/ChevronDown.svelte';
	import ChevronUp from '$lib/components/icons/ChevronUp.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import ArrowUp from '$lib/components/icons/ArrowUpCircle.svelte';
	import ArrowPath from '$lib/components/icons/ArrowPath.svelte';
	import OpenCodeChanges from '../Messages/ResponseMessage/OpenCodeChanges.svelte';

	const i18n = getContext('i18n');
	export let chatId = '';
	export let active = false;
	export let autoCompaction = true;
	export let onCompactionChange: (enabled: boolean) => void | Promise<void> = () => {};
	let state: AgentActivity = {
		session_id: null,
		busy: false,
		cursor: 0,
		items: [],
		requests: [],
		tools: []
	};
	let items: AgentActivity['items'] = [];
	let expanded = false;
	let tab = 'activity';
	let error = '';
	let loading = false;
	let action = '';
	let answers: Record<string, string> = {};
	let customAnswers: Record<string, string> = {};
	const customOption = '\u0000custom';
	const isOtherOption = (option: string) =>
		option.trim().toLocaleLowerCase() === $i18n.t('Other').toLocaleLowerCase() ||
		/^(?:others?|其他|其它|otr[oa]s?)(?:\s|[(:：,，.（/／-]|$)/iu.test(option.trim());
	const isCustomAnswer = (request: AgentInteraction) =>
		request.method === 'select' &&
		(answers[request.id] === customOption || isOtherOption(answers[request.id] ?? ''));
	let instruction = '';
	let delivery: 'steer' | 'follow_up' = 'steer';
	let boundChat = '';
	let timer: ReturnType<typeof setTimeout>;
	let destroyed = false;
	let generation = 0;
	let filter = '';
	$: pending = state.requests[0];
	$: usage = state.stats?.contextUsage;
	$: filteredItems = items.filter(
		(item) => !filter || JSON.stringify(item).toLowerCase().includes(filter.toLowerCase())
	);
	$: if (chatId !== boundChat) {
		boundChat = chatId;
		generation += 1;
		clearTimeout(timer);
		state = { session_id: null, busy: false, cursor: 0, items: [], requests: [], tools: [] };
		items = [];
		answers = {};
		customAnswers = {};
		error = '';
		loading = false;
		void refresh(generation);
	}
	const refresh = async (version = generation) => {
		if (destroyed || loading || !chatId || chatId.startsWith('local:')) return;
		clearTimeout(timer);
		const requestedChat = chatId;
		loading = true;
		try {
			let next = await getAgentActivity(localStorage.token, requestedChat, state.cursor);
			if (next.session_id !== state.session_id && state.cursor)
				next = await getAgentActivity(localStorage.token, requestedChat);
			if (version !== generation || destroyed) return;
			if (next.session_id !== state.session_id || next.truncated) items = [];
			const known = new Set(items.map((item) => item.id));
			items = [...items, ...next.items.filter((item) => !known.has(item.id))].slice(-500);
			state = next;
			error = '';
		} catch (value) {
			if (version === generation && !destroyed) error = String(value);
		} finally {
			if (version === generation && !destroyed) {
				loading = false;
				timer = setTimeout(
					() => refresh(version),
					active || state.busy || state.compacting || pending || expanded ? 1000 : 4000
				);
			}
		}
	};
	const run = async (
		kind: Parameters<typeof controlAgent>[2] | 'abort',
		values: Record<string, unknown> = {}
	) => {
		if (action && kind !== 'abort') return;
		action = kind;
		error = '';
		try {
			if (kind === 'abort') await abortOpenCodeChat(localStorage.token, chatId);
			else await controlAgent(localStorage.token, chatId, kind, values);
			if (kind === 'steer' || kind === 'follow_up') instruction = '';
			await refresh();
		} catch (value) {
			error = String(value);
		} finally {
			if (action === kind) action = '';
		}
	};
	const reply = (request: AgentInteraction, values: Record<string, unknown>) =>
		run('reply', { id: request.id, ...values });
	const submitAnswer = (request: AgentInteraction) => {
		const custom = isCustomAnswer(request);
		const value = custom
			? (customAnswers[request.id] ?? '').trim()
			: (answers[request.id] ?? request.prefill ?? '');
		if (request.method === 'select' && !value) return;
		return reply(request, { value, ...(custom ? { custom: true } : {}) });
	};
	const labels: Record<string, string> = {
		prompt: 'Task',
		started: 'Agent is working',
		message: 'Agent response',
		tool: 'Tool results',
		question: 'Waiting for your answer',
		answer: 'Answer submitted',
		settled: 'Agent task completed',
		aborted: 'Stopped',
		compaction_start: 'Compacting context',
		compaction_end: 'Context compaction finished',
		error: 'Failed',
		steer: 'New instruction',
		follow_up: 'Queued instruction',
		queue_cleared: 'Queue cleared',
		auto_retry_start: 'Retrying',
		auto_retry_end: 'Retry finished',
		diagnostic: 'Diagnostics',
		notice: 'Notification',
		plan: 'Task plan',
		extension_error: 'Extension error'
	};
	onDestroy(() => {
		destroyed = true;
		clearTimeout(timer);
	});
	onMount(() => {
		const update = (event: CustomEvent<{ chatId?: string }>) => {
			if (!event.detail?.chatId || event.detail.chatId === chatId) void refresh();
		};
		window.addEventListener('agent:interaction', update as EventListener);
		return () => window.removeEventListener('agent:interaction', update as EventListener);
	});
</script>

<section
	class="mb-2 border-b border-gray-200 px-1 pb-2 text-xs text-gray-700 dark:border-gray-800 dark:text-gray-300"
	aria-label="Agent"
>
	<div class="flex min-h-9 flex-wrap items-center gap-2">
		<button
			type="button"
			class="flex items-center gap-1.5 py-1 font-medium"
			on:click={() => {
				expanded = !expanded;
				if (expanded) void refresh();
			}}
			aria-expanded={expanded}
		>
			{#if expanded}<ChevronUp className="size-4" />{:else}<ChevronDown className="size-4" />{/if}
			Agent
			<span class="font-normal text-gray-500"
				>{$i18n.t(
					pending
						? 'Waiting for your answer'
						: state.compacting
							? 'Compacting context'
							: state.busy || active
								? 'Running'
								: 'Idle'
				)}</span
			>
		</button>
		{#if usage}
			<span class="text-gray-500" title={$i18n.t('Context usage')}
				>{usage.tokens?.toLocaleString() ?? '—'} / {usage.contextWindow.toLocaleString()} tokens</span
			>
		{/if}
		<div class="flex-1"></div>
		{#if state.session_id}
			<button
				type="button"
				disabled={!!action || state.busy || active || state.compacting}
				class="rounded p-1.5 hover:bg-gray-100 disabled:opacity-40 dark:hover:bg-gray-800"
				title={$i18n.t('Compact context now')}
				aria-label={$i18n.t('Compact context now')}
				on:click={() => run('compact')}><ArrowPath className="size-4" /></button
			>
			{#if state.busy || active || state.compacting}
				<button
					type="button"
					disabled={!!action && action !== 'compact'}
					class="rounded p-1.5 text-red-600 hover:bg-red-50 dark:hover:bg-red-950"
					title={$i18n.t('Stop Agent')}
					aria-label={$i18n.t('Stop Agent')}
					on:click={() => run('abort')}><XMark className="size-4" /></button
				>
			{/if}
		{/if}
	</div>
	{#if pending}
		<form
			class="mb-2 border-l-2 border-amber-500 py-2 pl-3"
			on:submit|preventDefault={() => submitAnswer(pending)}
		>
			<div class="font-medium break-words">{pending.title || 'Agent'}</div>
			{#if pending.message}<pre
					class="my-2 max-h-44 overflow-auto whitespace-pre-wrap break-words font-sans text-xs">{pending.message}</pre>{/if}
			{#if pending.method === 'select'}
				<fieldset class="my-2 space-y-1">
					<legend class="sr-only">{pending.title}</legend>
					{#each pending.options ?? [] as option}
						<label class="flex items-start gap-2 py-1"
							><input
								type="radio"
								name={pending.id}
								value={option}
								bind:group={answers[pending.id]}
								disabled={!!action}
								class="mt-0.5"
							/><span class="break-words">{option}</span></label
						>
					{/each}
					{#if !(pending.options ?? []).some(isOtherOption)}
						<label class="flex items-start gap-2 py-1">
							<input
								type="radio"
								name={pending.id}
								value={customOption}
								bind:group={answers[pending.id]}
								disabled={!!action}
								class="mt-0.5"
							/>
							<span>{$i18n.t('Other')}</span>
						</label>
					{/if}
				</fieldset>
				{#if isCustomAnswer(pending)}
					<textarea
						aria-label={$i18n.t('Your answer')}
						placeholder={$i18n.t('Your answer')}
						class="my-2 w-full resize-y rounded-md border border-gray-200 bg-transparent p-2 dark:border-gray-700"
						rows="2"
						maxlength="20000"
						required
						disabled={!!action}
						bind:value={customAnswers[pending.id]}
					></textarea>
				{/if}
			{:else if pending.method !== 'confirm'}
				<textarea
					aria-label={pending.title || $i18n.t('Your answer')}
					class="my-2 w-full resize-y rounded-md border border-gray-200 bg-transparent p-2 dark:border-gray-700"
					rows={pending.method === 'editor' ? 5 : 2}
					maxlength="20000"
					placeholder={pending.placeholder ?? ''}
					value={answers[pending.id] ?? pending.prefill ?? ''}
					on:input={(event) => (answers[pending.id] = event.currentTarget.value)}
				></textarea>
			{/if}
			<div class="mt-2 flex flex-wrap gap-2">
				{#if pending.method === 'confirm'}
					<button
						type="button"
						disabled={!!action}
						class="rounded-md bg-gray-900 px-3 py-1.5 text-white dark:bg-gray-100 dark:text-gray-900"
						on:click={() => reply(pending, { confirmed: true })}>{$i18n.t('Approve')}</button
					>
					<button
						type="button"
						disabled={!!action}
						class="rounded-md px-3 py-1.5 hover:bg-gray-100 dark:hover:bg-gray-800"
						on:click={() => reply(pending, { confirmed: false })}>{$i18n.t('Decline')}</button
					>
				{:else}
					<button
						type="submit"
						disabled={!!action ||
							(pending.method === 'select' &&
								(isCustomAnswer(pending)
									? !customAnswers[pending.id]?.trim()
									: !answers[pending.id]))}
						class="rounded-md bg-gray-900 px-3 py-1.5 text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900"
						>{$i18n.t('Submit')}</button
					>
					<button
						type="button"
						disabled={!!action}
						class="rounded-md px-3 py-1.5 hover:bg-gray-100 dark:hover:bg-gray-800"
						on:click={() => reply(pending, { cancelled: true })}>{$i18n.t('Cancel')}</button
					>
				{/if}
				{#if state.requests.length > 1}<span class="self-center text-gray-500"
						>+{state.requests.length - 1}</span
					>{/if}
			</div>
		</form>
	{/if}
	{#if error}<div role="alert" class="py-1 text-red-600 dark:text-red-400">{error}</div>{/if}
	{#if expanded}
		<div
			class="flex flex-wrap items-center gap-3 border-b border-gray-100 py-2 dark:border-gray-800"
		>
			{#each ['activity', 'tools', 'changes'] as view}
				<button
					type="button"
					class="py-1"
					class:font-semibold={tab === view}
					aria-pressed={tab === view}
					on:click={() => (tab = view)}
					>{$i18n.t(
						view === 'activity' ? 'Activity' : view === 'tools' ? 'Tool results' : 'File changes'
					)}</button
				>
			{/each}
			<div class="flex-1"></div>
			<label class="flex items-center gap-1.5"
				><input
					type="checkbox"
					disabled={!!action}
					checked={autoCompaction}
					on:change={async (event) => {
						const enabled = event.currentTarget.checked;
						try {
							await onCompactionChange(enabled);
							if (state.session_id) await run('auto_compaction', { enabled });
						} catch (value) {
							error = String(value);
						}
					}}
				/>{$i18n.t('Auto compact context')}</label
			>
		</div>
		{#if state.todos?.length}
			<ol class="my-2 space-y-1" aria-label={$i18n.t('Task plan')}>
				{#each state.todos as step}<li class="flex gap-2">
						<span class="text-gray-500"
							>{step.status === 'completed' ? '✓' : step.status === 'in_progress' ? '→' : '·'}</span
						><span class:line-through={step.status === 'completed'}>{step.content}</span>
					</li>{/each}
			</ol>
		{/if}
		<div class="max-h-72 overflow-y-auto overscroll-contain py-2">
			{#if tab === 'activity'}
				<input
					type="search"
					aria-label={$i18n.t('Search activity')}
					placeholder={$i18n.t('Search activity')}
					bind:value={filter}
					class="mb-2 w-full rounded-md border border-gray-200 bg-transparent px-2 py-1.5 dark:border-gray-700"
				/>
				{#each filteredItems as item (item.id)}
					<details class="border-l border-gray-200 pb-2 pl-3 dark:border-gray-700">
						<summary class="cursor-pointer break-words"
							><time class="mr-2 text-gray-400"
								>{new Date(item.time * 1000).toLocaleTimeString()}</time
							>{$i18n.t(labels[item.kind] || item.kind)}{item.name
								? ` · ${piToolLabel(item.name, $i18n)}`
								: ''}{item.status ? ` · ${piStatusLabel(item.status, $i18n)}` : ''}</summary
						>
						{#if item.text || item.output || item.input || item.request}<pre
								class="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all text-xs">{item.text ||
									item.output ||
									JSON.stringify(item.input ?? item.request, null, 2)}</pre>{/if}
						{#if item.tokens_before}<span class="text-gray-500"
								>{item.tokens_before} → {item.tokens_after ?? '—'} tokens</span
							>{/if}
					</details>
				{/each}
				{#if !items.length}<p class="py-2 text-gray-500">{$i18n.t('No activity yet')}</p>{/if}
				{#if state.text}<pre
						class="border-l-2 border-green-500 pl-3 whitespace-pre-wrap break-words font-sans text-xs">{state.text}</pre>{/if}
			{:else if tab === 'tools'}
				{#each state.tools as tool (tool.id)}
					<details
						open={tool.status === 'running'}
						class="mb-2 border-b border-gray-100 pb-2 dark:border-gray-800"
					>
						<summary class="cursor-pointer"
							>{piToolLabel(tool.name, $i18n)} · {piStatusLabel(tool.status, $i18n)}</summary
						>
						<pre
							class="mt-1 max-h-28 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(
								tool.input,
								null,
								2
							)}</pre>
						{#if tool.output}<pre
								class="mt-2 max-h-52 overflow-auto whitespace-pre-wrap break-all text-xs">{tool.output}</pre>{/if}
					</details>
				{/each}
				{#if !state.tools.length}<p class="py-2 text-gray-500">
						{$i18n.t('No tool calls yet')}
					</p>{/if}
			{:else if state.session_id}
				{#key state.session_id}<OpenCodeChanges {chatId} />{/key}
			{/if}
		</div>
		{#if state.busy || active}
			<form
				class="flex items-end gap-2 border-t border-gray-100 pt-2 dark:border-gray-800"
				on:submit|preventDefault={() => run(delivery, { message: instruction })}
			>
				<textarea
					rows="2"
					maxlength="20000"
					bind:value={instruction}
					aria-label={$i18n.t('New instruction')}
					placeholder={$i18n.t('New instruction')}
					class="min-w-0 flex-1 resize-y rounded-md border border-gray-200 bg-transparent p-2 dark:border-gray-700"
				></textarea>
				<select
					aria-label={$i18n.t('Instruction delivery')}
					bind:value={delivery}
					class="max-w-36 rounded-md border-0 bg-transparent py-2 text-xs"
					><option value="steer">{$i18n.t('Guide current task')}</option><option value="follow_up"
						>{$i18n.t('After current task')}</option
					></select
				>
				<button
					type="submit"
					disabled={!!action || !instruction.trim()}
					class="rounded-md bg-gray-900 p-2 text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900"
					title={$i18n.t('Send')}
					aria-label={$i18n.t('Send')}><ArrowUp className="size-4" /></button
				>
			</form>
		{/if}
		{#if state.queue?.steering.length || state.queue?.followUp.length}
			<div class="mt-2 flex items-start gap-2">
				<ul class="min-w-0 flex-1 text-gray-500">
					{#each [...(state.queue?.steering ?? []), ...(state.queue?.followUp ?? [])] as message}<li
							class="truncate"
						>
							{message}
						</li>{/each}
				</ul>
				<button
					type="button"
					disabled={!!action}
					title={$i18n.t('Clear queued instructions')}
					aria-label={$i18n.t('Clear queued instructions')}
					on:click={() => run('clear_queue')}><XMark className="size-4" /></button
				>
			</div>
		{/if}
		{#if loading}<span class="sr-only">{$i18n.t('Loading...')}</span>{/if}
	{/if}
</section>
