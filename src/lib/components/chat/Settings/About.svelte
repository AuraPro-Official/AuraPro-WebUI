<script lang="ts">
	import { getVersionUpdates } from '$lib/apis';
	import { getOllamaVersion } from '$lib/apis/ollama';
	import { WEBUI_BUILD_HASH, WEBUI_VERSION } from '$lib/constants';
	import { WEBUI_NAME, config, showChangelog } from '$lib/stores';
	import { compareVersion } from '$lib/utils';
	import { onMount, getContext } from 'svelte';

	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext('i18n');

	let ollamaVersion = '';

	let updateAvailable = null;
	let version = {
		current: '',
		latest: ''
	};

	const checkForVersionUpdates = async () => {
		updateAvailable = null;
		version = await getVersionUpdates(localStorage.token).catch((error) => {
			return {
				current: WEBUI_VERSION,
				latest: WEBUI_VERSION
			};
		});

		console.log(version);

		updateAvailable = compareVersion(version.latest, version.current);
		console.log(updateAvailable);
	};

	onMount(async () => {
		ollamaVersion = await getOllamaVersion(localStorage.token).catch((error) => {
			return '';
		});

		if ($config?.features?.enable_version_update_check) {
			checkForVersionUpdates();
		}
	});
</script>

<div id="tab-about" class="flex flex-col h-full justify-between space-y-3 text-sm">
	<div class=" space-y-3 overflow-y-scroll max-h-[28rem] md:max-h-full">
		<div>
			<div class=" mb-2.5 text-sm font-medium flex space-x-2 items-center">
				<div>
					{$WEBUI_NAME}
					{$i18n.t('Version')}
				</div>
			</div>
			<div class="flex w-full justify-between items-center">
				<div class="flex flex-col text-xs text-gray-700 dark:text-gray-200">
					<div class="flex gap-1">
						<Tooltip content={WEBUI_BUILD_HASH}>
							v{WEBUI_VERSION}
						</Tooltip>

						{#if $config?.features?.enable_version_update_check}
							<a
								href="https://github.com/AuraPro-Official/AuraPro-WebUI/releases/tag/v{version.latest}"
								target="_blank" rel="noopener noreferrer"
							>
								{updateAvailable === null
									? $i18n.t('Checking for updates...')
									: updateAvailable
										? `(v${version.latest} ${$i18n.t('available!')})`
										: $i18n.t('(latest)')}
							</a>
						{/if}
					</div>

					<button
						class=" underline flex items-center space-x-1 text-xs text-gray-500 dark:text-gray-500"
						on:click={() => {
							showChangelog.set(true);
						}}
					>
						<div>{$i18n.t("See what's new")}</div>
					</button>
				</div>

				{#if $config?.features?.enable_version_update_check}
					<button
						class=" text-xs px-3 py-1.5 bg-gray-100 hover:bg-gray-200 dark:bg-gray-850 dark:hover:bg-gray-800 transition rounded-lg font-medium"
						on:click={() => {
							checkForVersionUpdates();
						}}
					>
						{$i18n.t('Check for updates')}
					</button>
				{/if}
			</div>
		</div>

		{#if ollamaVersion}
			<hr class=" border-gray-100/30 dark:border-gray-850/30" />

			<div>
				<div class=" mb-2.5 text-sm font-medium">{$i18n.t('Ollama Version')}</div>
				<div class="flex w-full">
					<div class="flex-1 text-xs text-gray-700 dark:text-gray-200">
						{ollamaVersion ?? 'N/A'}
					</div>
				</div>
			</div>
		{/if}

		<hr class=" border-gray-100/30 dark:border-gray-850/30" />

		<div class="flex flex-wrap gap-x-3 gap-y-1 text-xs">
			<a class="underline" href="https://docs.openwebui.com/" target="_blank" rel="noopener noreferrer">Documentation</a>
			<a class="underline" href="https://github.com/open-webui/open-webui" target="_blank" rel="noopener noreferrer">GitHub</a
			>
			<a class="underline" href="https://discord.gg/5rJgQTnV4s" target="_blank" rel="noopener noreferrer">Discord</a>
			<a class="underline" href="https://twitter.com/OpenWebUI" target="_blank" rel="noopener noreferrer">X</a>
		</div>

		<div class="mt-2 text-xs text-gray-400 dark:text-gray-500">
			Copyright (c) {new Date().getFullYear()}
			<a class="font-medium underline" href="https://openwebui.com/" target="_blank" rel="noopener noreferrer"
				>Open WebUI Inc.</a
			>
			<a
				class="underline"
				href="https://github.com/open-webui/open-webui/blob/main/LICENSE"
				target="_blank" rel="noopener noreferrer">All rights reserved.</a
			>
		</div>

		<div class="mt-2 text-xs text-gray-400 dark:text-gray-500">
			{$i18n.t('Created by')}
			<a class="font-medium underline" href="https://github.com/tjbck" target="_blank" rel="noopener noreferrer"
				>Timothy J. Baek</a
			>
		</div>

		<div class="mt-2 text-xs text-gray-400 dark:text-gray-500">
			Emoji graphics provided by
			<a class="underline" href="https://github.com/jdecked/twemoji" target="_blank" rel="noopener noreferrer">Twemoji</a>,
			licensed under
			<a class="underline" href="https://creativecommons.org/licenses/by/4.0/" target="_blank" rel="noopener noreferrer"
				>CC-BY 4.0</a
			>.
		</div>

		<hr class="border-gray-100/30 dark:border-gray-850/30" />

		<div class="text-xs text-gray-400 dark:text-gray-500">
			This is a customized Open WebUI distribution maintained by
			<a
				class="font-medium underline"
				href="https://github.com/AuraPro-Official/AuraPro-WebUI"
				target="_blank" rel="noopener noreferrer">AuraPro contributors</a
			>. It is not affiliated with, endorsed by, or maintained by Open WebUI Inc.
		</div>

		<div class="mt-2 text-xs text-gray-400 dark:text-gray-500">
			Distribution license information:
			<a
				href="https://github.com/AuraPro-Official/AuraPro-WebUI/blob/main/LICENSE"
				target="_blank" rel="noopener noreferrer"
				class="underline">LICENSE</a
			>,
			<a
				href="https://github.com/AuraPro-Official/AuraPro-WebUI/blob/main/LICENSE_NOTICE"
				target="_blank" rel="noopener noreferrer"
				class="underline">LICENSE_NOTICE</a
			>, and
			<a
				href="https://github.com/AuraPro-Official/AuraPro-WebUI/blob/main/LICENSE_HISTORY"
				target="_blank" rel="noopener noreferrer"
				class="underline">LICENSE_HISTORY</a
			>.
		</div>
	</div>
</div>
