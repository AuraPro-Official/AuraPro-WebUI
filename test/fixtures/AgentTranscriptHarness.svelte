<script lang="ts">
	import { setContext } from 'svelte';
	import { writable } from 'svelte/store';
	import i18next from 'i18next';
	import en from '../../src/lib/i18n/locales/en-US/translation.json';
	import zh from '../../src/lib/i18n/locales/zh-CN/translation.json';
	import StructuredOutputRenderer from '../../src/lib/components/chat/Messages/StructuredOutputRenderer.svelte';
	import { settings } from '../../src/lib/stores';
	import type { OutputItem } from '../../src/lib/components/chat/Messages/structuredOutput';
	import '../../src/tailwind.css';
	const instance = i18next.createInstance();
	instance.init({
		lng: 'zh-CN',
		fallbackLng: 'en-US',
		initImmediate: false,
		resources: { 'en-US': { translation: en }, 'zh-CN': { translation: zh } }
	});
	setContext('i18n', writable(instance));
	settings.set({ expandDetails: true });
	let output: OutputItem[] = JSON.parse(localStorage.agentTranscript || '[]');
	let done = false;
	Object.assign(window, {
		setAgentTranscript: (value: OutputItem[], completed = false) => {
			output = value;
			done = completed;
			localStorage.agentTranscript = JSON.stringify(value);
		}
	});
</script>

<main
	class="prose dark:prose-invert"
	style="max-width:850px;margin:24px auto;padding:16px;font-family:Arial,sans-serif;letter-spacing:0"
>
	<StructuredOutputRenderer id="agent-transcript" {output} {done} />
</main>
