<script lang="ts">
	import { setContext } from 'svelte';
	import { writable } from 'svelte/store';
	import i18next from 'i18next';
	import en from '../../src/lib/i18n/locales/en-US/translation.json';
	import zh from '../../src/lib/i18n/locales/zh-CN/translation.json';
	import tw from '../../src/lib/i18n/locales/zh-TW/translation.json';
	import es from '../../src/lib/i18n/locales/es-ES/translation.json';
	import EpubLibrary from '../../src/routes/(app)/epub/+page.svelte';
	import '../../src/tailwind.css';
	import '../../src/app.css';
	import 'tippy.js/dist/tippy.css';

	const params = new URLSearchParams(location.search);
	document.documentElement.classList.toggle('dark', params.get('theme') === 'dark');
	const instance = i18next.createInstance();
	instance.init({
		lng: params.get('lang') || 'en-US',
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
</script>

<div
	class="flex h-screen justify-end bg-white text-gray-700 dark:bg-gray-900 dark:text-gray-100"
	style="--sidebar-width:260px"
>
	<EpubLibrary />
</div>
