<script lang="ts">
	import { getContext } from 'svelte';

	import Info from '$lib/components/icons/Info.svelte';

	export let content = '';

	const i18n = getContext('i18n');

	const getErrorMessage = (error: unknown) => {
		if (!error) return '';
		if (typeof error === 'string') return error;
		if (typeof error !== 'object') return `${error}`;

		const value = error as {
			detail?: string;
			message?: string;
			error?: string | { message?: string };
		};
		if (value.detail) return value.detail;
		if (typeof value.error === 'object' && value.error?.message) return value.error.message;
		if (value.message) return value.message;
		if (typeof value.error === 'string') return value.error;

		return JSON.stringify(error);
	};

	const localizeErrorMessage = (error: unknown) => {
		const message = getErrorMessage(error);
		const contextSizeMatch = message.match(
			/request\s+\((\d+)\s+tokens\)\s+exceeds\s+the\s+available\s+context\s+size\s+\((\d+)\s+tokens\),?\s*try increasing it/i
		);

		if (contextSizeMatch) {
			return $i18n.t(
				'The current conversation is too long for this model context. Request: {{requestTokens}} tokens, limit: {{contextTokens}} tokens. Please shorten the conversation or increase the context size.',
				{
					requestTokens: contextSizeMatch[1],
					contextTokens: contextSizeMatch[2]
				}
			);
		}

		return message;
	};
</script>

<div class="flex my-2 gap-2.5 border px-4 py-3 border-red-600/10 bg-red-600/10 rounded-lg">
	<div class=" self-start mt-0.5">
		<Info className="size-5 text-red-700 dark:text-red-400" />
	</div>

	<div class=" self-center text-sm">
		{localizeErrorMessage(content)}
	</div>
</div>
