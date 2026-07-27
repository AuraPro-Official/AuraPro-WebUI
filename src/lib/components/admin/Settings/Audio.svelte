<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { createEventDispatcher, onMount, getContext } from 'svelte';

	import { getBackendConfig } from '$lib/apis';
	import {
		getAudioConfig,
		getModels as _getModels,
		getVoices as _getVoices,
		updateAudioConfig
	} from '$lib/apis/audio';
	import { config } from '$lib/stores';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import SensitiveInput from '$lib/components/common/SensitiveInput.svelte';
	import Textarea from '$lib/components/common/Textarea.svelte';
	import { TTS_RESPONSE_SPLIT } from '$lib/types';

	const dispatch = createEventDispatcher();
	const i18n = getContext('i18n');

	export let saveHandler: () => void;

	let TTS_ENGINE = '';
	let TTS_OPENAI_API_BASE_URL = '';
	let TTS_OPENAI_API_KEY = '';
	let TTS_OPENAI_PARAMS = '';
	let TTS_MODEL = '';
	let TTS_VOICE = '';
	let TTS_SPLIT_ON: TTS_RESPONSE_SPLIT = TTS_RESPONSE_SPLIT.PUNCTUATION;

	let STT_ENGINE = '';
	let STT_OPENAI_API_BASE_URL = '';
	let STT_OPENAI_API_KEY = '';
	let STT_OPENAI_API_REQUEST_FORMAT = 'multipart';
	let STT_MODEL = '';
	let STT_SUPPORTED_CONTENT_TYPES = '';
	let STT_WHISPER_MODEL = '';

	let AUDIO_CONFIG_SAVING = false;
	let STT_WHISPER_MODEL_LOADING = false;
	let voices: { id: string; name: string }[] = [];
	let models: { id: string; name?: string }[] = [];
	const SHERPA_AUDIO_BASE_URL = 'http://127.0.0.1:39384/v1';

	const isRemoteTTS = () => TTS_ENGINE === 'openai' || TTS_ENGINE === 'sherpa';
	const isRemoteSTT = () => STT_ENGINE === 'openai' || STT_ENGINE === 'sherpa';

	const normalizeAudioEngine = (engine: string) =>
		['', 'web', 'openai', 'sherpa', 'multimodal'].includes(engine) ? engine : '';

	const getToastError = (error: unknown) => {
		if (typeof error === 'string') {
			return error;
		}

		if (Array.isArray(error)) {
			return error
				.map((item) => (typeof item === 'string' ? item : JSON.stringify(item)))
				.join('\n');
		}

		if (error instanceof Error) {
			return error.message;
		}

		if (error && typeof error === 'object') {
			return JSON.stringify(error);
		}

		return $i18n.t('Failed to save settings. Please check if the service is running.');
	};

	const normalizeAudioApiBaseUrl = (url: string, engine: string) => {
		let value = (url ?? '').trim().replace(/\/+$/, '');
		if (!value) {
			return value;
		}

		if (!/^https?:\/\//i.test(value)) {
			value = `http://${value}`;
		}

		if (engine === 'sherpa' && !value.endsWith('/v1')) {
			value = `${value}/v1`;
		}

		return value;
	};

	const getModels = async () => {
		if (!isRemoteTTS()) {
			models = [];
			return;
		}

		const res = await _getModels(localStorage.token).catch((e) => toast.error(`${e}`));
		models = res?.models ?? [];
	};

	const getVoices = async () => {
		if (TTS_ENGINE === '') {
			const getVoicesLoop = setInterval(() => {
				const browserVoices = speechSynthesis.getVoices();
				if (browserVoices.length > 0) {
					clearInterval(getVoicesLoop);
					voices = browserVoices
						.sort((a, b) => a.name.localeCompare(b.name, $i18n.resolvedLanguage))
						.map((voice) => ({ id: voice.voiceURI, name: voice.name }));
				}
			}, 100);
			return;
		}

		if (!isRemoteTTS()) {
			voices = [];
			return;
		}

		const res = await _getVoices(localStorage.token).catch((e) => toast.error(`${e}`));
		voices = res?.voices ?? [];
	};

	const applyEngineDefaults = () => {
		if (TTS_ENGINE === 'sherpa') {
			TTS_OPENAI_API_BASE_URL = normalizeAudioApiBaseUrl(
				TTS_OPENAI_API_BASE_URL || SHERPA_AUDIO_BASE_URL,
				'sherpa'
			);
			TTS_MODEL = TTS_MODEL || 'sherpa-tts';
			TTS_VOICE = TTS_VOICE || '0';
			TTS_OPENAI_API_KEY = TTS_OPENAI_API_KEY || 'aurapro-local';
		} else if (TTS_ENGINE === 'openai') {
			TTS_MODEL = TTS_MODEL || 'tts-1';
			TTS_VOICE = TTS_VOICE || 'alloy';
		} else {
			TTS_MODEL = '';
			TTS_VOICE = '';
		}

		if (STT_ENGINE === 'sherpa') {
			STT_OPENAI_API_BASE_URL = normalizeAudioApiBaseUrl(
				STT_OPENAI_API_BASE_URL || SHERPA_AUDIO_BASE_URL,
				'sherpa'
			);
			STT_MODEL = STT_MODEL || 'sherpa-asr';
			STT_OPENAI_API_KEY = STT_OPENAI_API_KEY || 'aurapro-local';
		} else if (STT_ENGINE === 'openai') {
			STT_MODEL = STT_MODEL || 'whisper-1';
		}
	};

	const updateConfigHandler = async () => {
		if (AUDIO_CONFIG_SAVING) {
			return false;
		}

		AUDIO_CONFIG_SAVING = true;

		let openaiParams = {};
		try {
			openaiParams = TTS_OPENAI_PARAMS ? JSON.parse(TTS_OPENAI_PARAMS) : {};
			TTS_OPENAI_PARAMS = JSON.stringify(openaiParams, null, 2);
		} catch {
			toast.error($i18n.t('Invalid JSON format for Parameters'));
			AUDIO_CONFIG_SAVING = false;
			return false;
		}

		applyEngineDefaults();
		TTS_OPENAI_API_BASE_URL = normalizeAudioApiBaseUrl(TTS_OPENAI_API_BASE_URL, TTS_ENGINE);
		STT_OPENAI_API_BASE_URL = normalizeAudioApiBaseUrl(STT_OPENAI_API_BASE_URL, STT_ENGINE);

		try {
			const res = await updateAudioConfig(localStorage.token, {
				tts: {
					OPENAI_API_BASE_URL: TTS_OPENAI_API_BASE_URL,
					OPENAI_API_KEY: TTS_OPENAI_API_KEY,
					OPENAI_PARAMS: openaiParams,
					ENGINE: TTS_ENGINE,
					MODEL: TTS_MODEL,
					VOICE: TTS_VOICE,
					SPLIT_ON: TTS_SPLIT_ON
				},
				stt: {
					OPENAI_API_BASE_URL: STT_OPENAI_API_BASE_URL,
					OPENAI_API_KEY: STT_OPENAI_API_KEY,
					OPENAI_API_REQUEST_FORMAT: STT_OPENAI_API_REQUEST_FORMAT,
					ENGINE: STT_ENGINE,
					MODEL: STT_MODEL,
					SUPPORTED_CONTENT_TYPES: STT_SUPPORTED_CONTENT_TYPES.split(',')
						.map((type) => type.trim())
						.filter(Boolean),
					WHISPER_MODEL: STT_WHISPER_MODEL
				}
			});

			if (!res) {
				throw new Error(
					$i18n.t('Failed to save settings. Please check if the service is running.')
				);
			}

			if (res) {
				saveHandler();
				await config.set(await getBackendConfig());
				return true;
			}
		} catch (e) {
			console.error('Failed to save audio config:', e);
			toast.error(getToastError(e));
			return false;
		} finally {
			AUDIO_CONFIG_SAVING = false;
		}
	};

	const sttModelUpdateHandler = async () => {
		STT_WHISPER_MODEL_LOADING = true;
		await updateConfigHandler();
		STT_WHISPER_MODEL_LOADING = false;
	};

	onMount(async () => {
		const res = await getAudioConfig(localStorage.token);
		if (!res) return;

		TTS_ENGINE = normalizeAudioEngine(res.tts.ENGINE);
		TTS_OPENAI_API_BASE_URL = res.tts.OPENAI_API_BASE_URL;
		TTS_OPENAI_API_KEY = res.tts.OPENAI_API_KEY;
		TTS_OPENAI_PARAMS = JSON.stringify(res?.tts?.OPENAI_PARAMS ?? {}, null, 2);
		TTS_MODEL = res.tts.MODEL;
		TTS_VOICE = res.tts.VOICE;
		TTS_SPLIT_ON = res.tts.SPLIT_ON || TTS_RESPONSE_SPLIT.PUNCTUATION;

		STT_ENGINE = normalizeAudioEngine(res.stt.ENGINE);
		STT_OPENAI_API_BASE_URL = res.stt.OPENAI_API_BASE_URL;
		STT_OPENAI_API_KEY = res.stt.OPENAI_API_KEY;
		STT_OPENAI_API_REQUEST_FORMAT = res.stt.OPENAI_API_REQUEST_FORMAT || 'multipart';
		STT_MODEL = res.stt.MODEL;
		STT_SUPPORTED_CONTENT_TYPES = (res?.stt?.SUPPORTED_CONTENT_TYPES ?? []).join(',');
		STT_WHISPER_MODEL = res.stt.WHISPER_MODEL;

		applyEngineDefaults();
		await getVoices();
		await getModels();
	});
</script>

<form
	class="flex flex-col h-full justify-between space-y-3 text-sm"
	on:submit|preventDefault={async () => {
		if (await updateConfigHandler()) {
			dispatch('save');
		}
	}}
>
	<div class="space-y-6 overflow-y-scroll scrollbar-hidden h-full">
		<section class="space-y-3">
			<div class="mt-0.5 text-base font-medium">{$i18n.t('Speech-to-Text')}</div>
			<hr class="border-gray-100/30 dark:border-gray-850/30" />

			<div class="flex items-center justify-between">
				<div class="text-xs font-medium">{$i18n.t('Speech-to-Text Engine')}</div>
				<select
					class="cursor-pointer w-fit pr-8 rounded-sm px-2 p-1 text-xs bg-transparent outline-hidden text-right"
					bind:value={STT_ENGINE}
					on:change={applyEngineDefaults}
				>
					<option value="">{$i18n.t('Whisper (Local)')}</option>
					<option value="web">{$i18n.t('Web API')}</option>
					<option value="openai">{$i18n.t('OpenAI API')}</option>
					<option value="sherpa">Sherpa</option>
					<option value="multimodal">{$i18n.t('Multimodal')}</option>
				</select>
			</div>

			{#if STT_ENGINE !== 'web' && STT_ENGINE !== 'multimodal'}
				<div>
					<div class="mb-1.5 text-xs font-medium">{$i18n.t('Supported MIME Types')}</div>
					<input
						class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
						bind:value={STT_SUPPORTED_CONTENT_TYPES}
						placeholder={$i18n.t('e.g., audio/wav,audio/mpeg,video/* (leave blank for defaults)')}
					/>
				</div>
			{/if}

			{#if STT_ENGINE === ''}
				<div>
					<div class="mb-1.5 text-xs font-medium">{$i18n.t('STT Model')}</div>
					<div class="flex w-full gap-2">
						<input
							class="flex-1 rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
							placeholder={$i18n.t('Set whisper model')}
							bind:value={STT_WHISPER_MODEL}
						/>
						<button
							class="px-2.5 bg-gray-50 hover:bg-gray-200 text-gray-800 dark:bg-gray-850 dark:hover:bg-gray-800 dark:text-gray-100 rounded-lg transition"
							on:click={sttModelUpdateHandler}
							disabled={STT_WHISPER_MODEL_LOADING}
							type="button"
						>
							{#if STT_WHISPER_MODEL_LOADING}<Spinner />{:else}{$i18n.t('Save')}{/if}
						</button>
					</div>
				</div>
			{:else if isRemoteSTT()}
				<div class="flex gap-2">
					<input
						class="flex-1 w-full bg-transparent outline-hidden"
						placeholder={$i18n.t('API Base URL')}
						bind:value={STT_OPENAI_API_BASE_URL}
					/>
					<SensitiveInput placeholder={$i18n.t('API Key')} bind:value={STT_OPENAI_API_KEY} />
				</div>
				{#if STT_ENGINE === 'openai'}
					<div class="flex items-center justify-between">
						<div class="text-xs font-medium">{$i18n.t('Request Format')}</div>
						<select
							class="cursor-pointer w-fit pr-8 rounded-sm px-2 p-1 text-xs bg-transparent outline-hidden text-right"
							bind:value={STT_OPENAI_API_REQUEST_FORMAT}
						>
							<option value="multipart">{$i18n.t('Multipart Upload')}</option>
							<option value="json">{$i18n.t('JSON Base64')}</option>
						</select>
					</div>
				{/if}
				<div>
					<div class="mb-1.5 text-xs font-medium">{$i18n.t('STT Model')}</div>
					<input
						class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
						bind:value={STT_MODEL}
						placeholder={STT_ENGINE === 'sherpa' ? 'sherpa-asr' : 'whisper-1'}
					/>
				</div>
			{/if}
		</section>

		<section class="space-y-3">
			<div class="mt-0.5 text-base font-medium">{$i18n.t('Text-to-Speech')}</div>
			<hr class="border-gray-100/30 dark:border-gray-850/30" />

			<div class="flex items-center justify-between">
				<div class="text-xs font-medium">{$i18n.t('Text-to-Speech Engine')}</div>
				<select
					class="w-fit pr-8 cursor-pointer rounded-sm px-2 p-1 text-xs bg-transparent outline-hidden text-right"
					bind:value={TTS_ENGINE}
					on:change={async () => {
						applyEngineDefaults();
						await getVoices();
						await getModels();
					}}
				>
					<option value="">{$i18n.t('Web API')}</option>
					<option value="openai">{$i18n.t('OpenAI API')}</option>
					<option value="sherpa">Sherpa</option>
				</select>
			</div>

			{#if isRemoteTTS()}
				<div class="flex gap-2">
					<input
						class="flex-1 w-full bg-transparent outline-hidden"
						placeholder={$i18n.t('API Base URL')}
						bind:value={TTS_OPENAI_API_BASE_URL}
					/>
					<SensitiveInput placeholder={$i18n.t('API Key')} bind:value={TTS_OPENAI_API_KEY} />
				</div>

				<div class="flex gap-2">
					<div class="w-full">
						<div class="mb-1.5 text-xs font-medium">{$i18n.t('TTS Voice')}</div>
						<input
							list="voice-list"
							class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
							bind:value={TTS_VOICE}
							placeholder={TTS_ENGINE === 'sherpa' ? '0' : $i18n.t('Select a voice')}
						/>
						<datalist id="voice-list">
							{#each voices as voice}
								<option value={voice.id}>{voice.name}</option>
							{/each}
						</datalist>
					</div>
					<div class="w-full">
						<div class="mb-1.5 text-xs font-medium">{$i18n.t('TTS Model')}</div>
						<input
							list="tts-model-list"
							class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
							bind:value={TTS_MODEL}
							placeholder={TTS_ENGINE === 'sherpa' ? 'sherpa-tts' : $i18n.t('Select a model')}
						/>
						<datalist id="tts-model-list">
							{#each models as model}
								<option value={model.id} class="bg-gray-50 dark:bg-gray-700" />
							{/each}
						</datalist>
					</div>
				</div>

				{#if TTS_ENGINE === 'openai'}
					<div>
						<div class="mb-1.5 text-xs font-medium">{$i18n.t('Additional Parameters')}</div>
						<Textarea
							className="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
							bind:value={TTS_OPENAI_PARAMS}
							placeholder={$i18n.t('Enter additional parameters in JSON format')}
							minSize={100}
						/>
					</div>
				{/if}
			{:else}
				<div>
					<div class="mb-1.5 text-xs font-medium">{$i18n.t('TTS Voice')}</div>
					<select
						class="w-full rounded-lg py-2 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden"
						bind:value={TTS_VOICE}
					>
						<option value="">{$i18n.t('Default')}</option>
						{#each voices as voice}
							<option value={voice.id}>{voice.name}</option>
						{/each}
					</select>
				</div>
			{/if}

			<div class="flex items-center justify-between">
				<div class="text-xs font-medium">{$i18n.t('Response splitting')}</div>
				<select
					class="w-fit pr-8 cursor-pointer rounded-sm px-2 p-1 text-xs bg-transparent outline-hidden text-right"
					aria-label={$i18n.t('Select how to split message text for TTS requests')}
					bind:value={TTS_SPLIT_ON}
				>
					{#each Object.values(TTS_RESPONSE_SPLIT) as split}
						<option value={split}>{$i18n.t(split.charAt(0).toUpperCase() + split.slice(1))}</option>
					{/each}
				</select>
			</div>
		</section>
	</div>

	<div class="flex justify-end text-sm font-medium">
		<button
			class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full disabled:cursor-not-allowed disabled:opacity-60"
			type="submit"
			disabled={AUDIO_CONFIG_SAVING}
		>
			{#if AUDIO_CONFIG_SAVING}
				<div class="flex items-center gap-1.5">
					<Spinner />
					<span>{$i18n.t('Saving...')}</span>
				</div>
			{:else}
				{$i18n.t('Save')}
			{/if}
		</button>
	</div>
</form>
