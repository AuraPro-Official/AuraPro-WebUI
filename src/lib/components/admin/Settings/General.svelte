<script lang="ts">
	import { v4 as uuidv4 } from 'uuid';

	import { getBackendConfig, getVersionUpdates } from '$lib/apis';
	import { getAdminConfig, updateAdminConfig } from '$lib/apis/auths';
	import { getBanners, setBanners } from '$lib/apis/configs';
	import Switch from '$lib/components/common/Switch.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { WEBUI_BUILD_HASH, WEBUI_VERSION } from '$lib/constants';
	import { banners as _banners, config, showChangelog } from '$lib/stores';
	import type { Banner } from '$lib/types';
	import { compareVersion } from '$lib/utils';
	import { onMount, getContext } from 'svelte';
	import { toast } from 'svelte-sonner';
	import Textarea from '$lib/components/common/Textarea.svelte';
	import Banners from './Interface/Banners.svelte';
	import Events from './Events.svelte';

	const i18n = getContext('i18n');

	export let saveHandler: Function;

	let updateAvailable = false;
	let version = {
		current: WEBUI_VERSION,
		latest: WEBUI_VERSION
	};

	let adminConfig = null;
	let memoryUpdateNotifications = true;
	let memoryReviewInterval = 6;
	let memoryReviewModel = '';

	let banners: Banner[] = [];

	const checkForVersionUpdates = async () => {
		updateAvailable = null;
		version = await getVersionUpdates(localStorage.token).catch((error) => {
			return {
				current: WEBUI_VERSION,
				latest: WEBUI_VERSION
			};
		});

		console.info(version);

		updateAvailable = compareVersion(version.latest, version.current);
		console.info(updateAvailable);
	};

	const updateBanners = async () => {
		_banners.set(await setBanners(localStorage.token, banners));
	};

	const updateHandler = async () => {
		const res = await updateAdminConfig(localStorage.token, {
			...adminConfig,
			ENABLE_MEMORY_UPDATE_NOTIFICATIONS: memoryUpdateNotifications,
			MEMORIES_REVIEW_INTERVAL_TURNS: memoryReviewInterval,
			MEMORIES_REVIEW_MODEL: memoryReviewModel
		});

		await updateBanners();

		await config.set(await getBackendConfig());

		if (res) {
			saveHandler();
		} else {
			toast.error($i18n.t('Failed to update settings'));
		}
	};

	onMount(async () => {
		const loadedAdminConfig = await getAdminConfig(localStorage.token);
		adminConfig = loadedAdminConfig;
		memoryUpdateNotifications = loadedAdminConfig?.ENABLE_MEMORY_UPDATE_NOTIFICATIONS ?? true;
		memoryReviewInterval = loadedAdminConfig?.MEMORIES_REVIEW_INTERVAL_TURNS ?? 6;
		memoryReviewModel = loadedAdminConfig?.MEMORIES_REVIEW_MODEL ?? '';

		banners = [...$_banners];
	});
</script>

<form
	class="flex flex-col h-full justify-between space-y-3 text-sm"
	on:submit|preventDefault={async () => {
		updateHandler();
	}}
>
	<div class="space-y-3 overflow-y-scroll scrollbar-hidden h-full">
		{#if adminConfig !== null}
			<div class="">
				<div class="mb-3.5">
					<div class=" mt-0.5 mb-2.5 text-base font-medium">{$i18n.t('General')}</div>

					<hr class=" border-gray-100/30 dark:border-gray-850/30 my-2" />

					<div class="mb-2.5">
						<div class=" mb-1 text-xs font-medium flex space-x-2 items-center">
							<div>
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
											target="_blank"
											rel="noopener noreferrer"
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
									type="button"
									on:click={() => {
										showChangelog.set(true);
									}}
								>
									<div>{$i18n.t("See what's new")}</div>
								</button>
							</div>

							{#if $config?.features?.enable_version_update_check}
								<button
									class=" text-xs px-3 py-1.5 bg-gray-50 hover:bg-gray-100 dark:bg-gray-850 dark:hover:bg-gray-800 transition rounded-lg font-medium"
									type="button"
									on:click={() => {
										checkForVersionUpdates();
									}}
								>
									{$i18n.t('Check for updates')}
								</button>
							{/if}
						</div>
					</div>

					<div class="mb-2.5">
						<div class="flex w-full justify-between items-center">
							<div class="text-xs pr-2">
								<div class="">
									{$i18n.t('Help')}
								</div>
								<div class=" text-xs text-gray-500">
									{$i18n.t('Discover how to use Open WebUI and seek support from the community.')}
								</div>
							</div>

							<a
								class="flex-shrink-0 text-xs font-medium underline"
								href="https://docs.openwebui.com/"
								target="_blank"
								rel="noopener noreferrer"
							>
								{$i18n.t('Documentation')}
							</a>
						</div>

						<div class="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs">
							<a
								class="underline"
								href="https://github.com/open-webui/open-webui"
								target="_blank"
								rel="noopener noreferrer">GitHub</a
							>
							<a
								class="underline"
								href="https://discord.gg/5rJgQTnV4s"
								target="_blank"
								rel="noopener noreferrer">Discord</a
							>
							<a
								class="underline"
								href="https://twitter.com/OpenWebUI"
								target="_blank"
								rel="noopener noreferrer">X</a
							>
						</div>

						<div class="mt-2 text-xs text-gray-500">
							Open WebUI was created by
							<a
								href="https://github.com/tjbck"
								target="_blank"
								rel="noopener noreferrer"
								class="underline">Timothy J. Baek</a
							>
							and is maintained by
							<a
								href="https://openwebui.com/"
								target="_blank"
								rel="noopener noreferrer"
								class="underline">Open WebUI Inc.</a
							>
						</div>

						<div
							class="mt-2 border-t border-gray-100/30 pt-2 text-xs text-gray-500 dark:border-gray-850/30"
						>
							This customized distribution is maintained by
							<a
								href="https://github.com/AuraPro-Official/AuraPro-WebUI"
								target="_blank"
								rel="noopener noreferrer"
								class="underline">AuraPro contributors</a
							>
							and is not affiliated with or endorsed by Open WebUI Inc. See
							<a
								href="https://github.com/AuraPro-Official/AuraPro-WebUI/blob/main/LICENSE_NOTICE"
								target="_blank"
								rel="noopener noreferrer"
								class="underline">LICENSE_NOTICE</a
							>
							for upstream license attribution.
						</div>
					</div>
				</div>

				<div class="mb-3">
					<div class=" mt-0.5 mb-2.5 text-base font-medium">{$i18n.t('Features')}</div>

					<hr class=" border-gray-100/30 dark:border-gray-850/30 my-2" />

					<div class="mb-2.5 flex w-full items-center justify-between pr-2">
						<div class=" self-center text-xs font-medium">
							{$i18n.t('Folders')}
						</div>

						<Switch bind:state={adminConfig.ENABLE_FOLDERS} />
					</div>

					{#if adminConfig.ENABLE_FOLDERS}
						<div class="mb-2.5 w-full justify-between">
							<div class="flex w-full justify-between">
								<div class=" self-center text-xs font-medium">
									{$i18n.t('Folder Max File Count')}
								</div>
							</div>

							<div class="flex mt-2 space-x-2">
								<input
									class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
									type="number"
									min="0"
									placeholder={$i18n.t('Leave empty for unlimited')}
									bind:value={adminConfig.FOLDER_MAX_FILE_COUNT}
								/>
							</div>

							<div class="mt-2 text-xs text-gray-400 dark:text-gray-500">
								{$i18n.t('Maximum number of files allowed per folder.')}
							</div>
						</div>
					{/if}

					<div class="mb-2.5 flex w-full items-center justify-between pr-2">
						<div class=" self-center text-xs font-medium">
							{$i18n.t('Memories')}
						</div>

						<Switch bind:state={adminConfig.ENABLE_MEMORIES} />
					</div>

					{#if adminConfig.ENABLE_MEMORIES}
						<div class="mb-2.5 flex w-full items-center justify-between pr-2 pl-4">
							<div class=" self-center text-xs font-medium text-gray-500 dark:text-gray-400">
								{$i18n.t('Memory System Context')}
							</div>

							<Switch bind:state={adminConfig.ENABLE_MEMORY_SYSTEM_CONTEXT} />
						</div>

						<div class="mb-2.5 flex w-full items-center justify-between pr-2 pl-4">
							<div class=" self-center text-xs font-medium text-gray-500 dark:text-gray-400">
								{$i18n.t('Memory Background Review')}
							</div>

							<Switch bind:state={adminConfig.ENABLE_MEMORY_BACKGROUND_REVIEW} />
						</div>

						<div class="mb-2.5 flex w-full items-center justify-between pr-2 pl-8">
							<div class="self-center text-xs text-gray-500 dark:text-gray-400">
								{$i18n.t('Memory update notifications')}
							</div>
							<Switch bind:state={memoryUpdateNotifications} />
						</div>

						<div class="mb-2.5 pl-8 pr-2">
							<div class="text-xs font-medium text-gray-500 dark:text-gray-400">
								{$i18n.t('Fallback review interval')}
							</div>
							<input
								class="mt-1 w-full rounded-lg py-2 px-3 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
								type="number"
								min="1"
								max="50"
								bind:value={memoryReviewInterval}
							/>
							<div class="mt-1 text-xs text-gray-400 dark:text-gray-500">
								{$i18n.t(
									'Durable information is reviewed immediately. This interval only checks conversations without a clear memory signal.'
								)}
							</div>
						</div>

						<div class="mb-2.5 pl-8 pr-2">
							<div class="text-xs font-medium text-gray-500 dark:text-gray-400">
								{$i18n.t('Memory review model')}
							</div>
							<input
								class="mt-1 w-full rounded-lg py-2 px-3 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
								type="text"
								bind:value={memoryReviewModel}
								placeholder={$i18n.t('Leave empty to use the current chat model')}
							/>
						</div>
					{/if}

					<div class="mb-2.5 flex w-full items-center justify-between pr-2">
						<div class=" self-center text-xs font-medium">
							{$i18n.t('Notes')}
						</div>

						<Switch bind:state={adminConfig.ENABLE_NOTES} />
					</div>

					<div class="mb-2.5 flex w-full items-center justify-between pr-2">
						<div class=" self-center text-xs font-medium">
							{$i18n.t('Channels')}
						</div>

						<Switch bind:state={adminConfig.ENABLE_CHANNELS} />
					</div>

					<div class="mb-2.5 flex w-full items-center justify-between pr-2">
						<div class=" self-center text-xs font-medium">
							{$i18n.t('Calendar')}
						</div>

						<Switch bind:state={adminConfig.ENABLE_CALENDAR} />
					</div>

					<div class="mb-2.5 flex w-full items-center justify-between pr-2">
						<div class=" self-center text-xs font-medium">
							{$i18n.t('Automations')}
						</div>

						<Switch bind:state={adminConfig.ENABLE_AUTOMATIONS} />
					</div>

					<div class="mb-2.5 flex w-full items-center justify-between pr-2">
						<div class=" self-center text-xs font-medium">
							{$i18n.t('User Webhooks')}
						</div>

						<Switch bind:state={adminConfig.ENABLE_USER_WEBHOOKS} />
					</div>

					<div class="mb-2.5 flex w-full items-center justify-between pr-2">
						<div class=" self-center text-xs font-medium">
							{$i18n.t('User Status')}
						</div>

						<Switch bind:state={adminConfig.ENABLE_USER_STATUS} />
					</div>

					<div class="mb-2.5">
						<div class=" self-center text-xs font-medium mb-2">
							{$i18n.t('Response Watermark')}
						</div>
						<Textarea
							placeholder={$i18n.t('Enter a watermark for the response. Leave empty for none.')}
							bind:value={adminConfig.RESPONSE_WATERMARK}
						/>
					</div>

					<div class="mb-2.5 w-full justify-between">
						<div class="flex w-full justify-between">
							<div class=" self-center text-xs font-medium">{$i18n.t('WebUI URL')}</div>
						</div>

						<div class="flex mt-2 space-x-2">
							<input
								class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
								type="text"
								placeholder={`e.g.) "http://localhost:3000"`}
								bind:value={adminConfig.WEBUI_URL}
							/>
						</div>

						<div class="mt-2 text-xs text-gray-400 dark:text-gray-500">
							{$i18n.t(
								'Enter the public URL of your WebUI. This URL will be used to generate links in the notifications.'
							)}
						</div>
					</div>
				</div>

				<Events />

				<div class="mb-3.5">
					<div class=" mt-0.5 mb-2.5 text-base font-medium">{$i18n.t('UI')}</div>

					<hr class=" border-gray-100/30 dark:border-gray-850/30 my-2" />

					<div class="mb-2.5">
						<div class="flex w-full justify-between">
							<div class=" self-center text-xs">
								{$i18n.t('Banners')}
							</div>

							<button
								class="p-1 px-3 text-xs flex rounded-sm transition"
								type="button"
								on:click={() => {
									if (banners.length === 0 || banners.at(-1).content !== '') {
										banners = [
											...banners,
											{
												id: uuidv4(),
												type: '',
												title: '',
												content: '',
												dismissible: true,
												timestamp: Math.floor(Date.now() / 1000)
											}
										];
									}
								}}
							>
								<svg
									xmlns="http://www.w3.org/2000/svg"
									viewBox="0 0 20 20"
									fill="currentColor"
									class="w-4 h-4"
								>
									<path
										d="M10.75 4.75a.75.75 0 00-1.5 0v4.5h-4.5a.75.75 0 000 1.5h4.5v4.5a.75.75 0 001.5 0v-4.5h4.5a.75.75 0 000-1.5h-4.5v-4.5z"
									/>
								</svg>
							</button>
						</div>

						<Banners bind:banners />
					</div>
				</div>
			</div>
		{/if}
	</div>

	<div class="flex justify-end pt-3 text-sm font-medium">
		<button
			class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full"
			type="submit"
		>
			{$i18n.t('Save')}
		</button>
	</div>
</form>
