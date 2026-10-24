<script lang="ts">
	import { setContext } from 'svelte';
	import { writable } from 'svelte/store';
	import type { OpenCodeChatConfig } from '../../src/lib/apis/opencode';
	import i18next from 'i18next';
	import en from '../../src/lib/i18n/locales/en-US/translation.json';
	import zh from '../../src/lib/i18n/locales/zh-CN/translation.json';
	import tw from '../../src/lib/i18n/locales/zh-TW/translation.json';
	import es from '../../src/lib/i18n/locales/es-ES/translation.json';
	import AgentWorkspace from '../../src/lib/components/chat/MessageInput/AgentWorkspace.svelte';
	import AgentControls from '../../src/lib/components/chat/MessageInput/AgentControls.svelte';
	import '../../src/tailwind.css';
	const instance = i18next.createInstance();
	instance.init({
		lng: new URLSearchParams(location.search).get('lang') || 'en-US',
		fallbackLng: 'en-US',
		initImmediate: false,
		resources: {
			'en-US': { translation: en },
			'zh-CN': { translation: zh },
			'zh-TW': { translation: tw },
			'es-ES': { translation: es }
		}
	});
	setContext('i18n', writable(instance));
	let autoCompaction = true;
	let config: OpenCodeChatConfig = {
		enabled: true,
		directory: 'D:/projects/example',
		agent: 'build',
		model: '',
		approval_mode: 'task'
	};
</script>

<main
	style="max-width:900px;margin:24px auto;padding:16px;font-family:Arial,sans-serif;letter-spacing:0"
>
	<AgentWorkspace
		chatId="test-chat"
		active={new URLSearchParams(location.search).get('active') !== '0'}
		{autoCompaction}
		onCompactionChange={(value) => {
			autoCompaction = value;
		}}
	/>
	<AgentControls
		{config}
		onChange={(value) => {
			config = value;
		}}
	/>
</main>
