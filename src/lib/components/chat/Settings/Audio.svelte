<script lang="ts">
	import { createEventDispatcher, onMount, getContext } from 'svelte';
	import { settings, config } from '$lib/stores';
	import { getVoices as _getVoices } from '$lib/apis/audio';
	import { toast } from 'svelte-sonner';
	import Switch from '$lib/components/common/Switch.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import {
		groupAudioDevices,
		hasAnonymousAudioDevices,
		reconcileAudioDeviceId
	} from '$lib/utils/audio-devices';

	const dispatch = createEventDispatcher();
	const i18n = getContext('i18n');

	export let saveSettings: (settings: Record<string, unknown>) => void | Promise<void>;

	let speechAutoSend = false;
	let responseAutoPlayback = false;
	let nonLocalVoices = false;
	let STTEngine = '';
	let STTLanguage = '';
	let TTSEngine = '';
	let TTSEngineConfig: { dtype: string } = { dtype: '' };
	let voices = [];
	let voice = '';
	let playbackRate = 1;
	let audioInputDeviceId = '';
	let audioOutputDeviceId = '';
	let simultaneousMode = 'off';
	let audioInputDevices: MediaDeviceInfo[] = [];
	let audioOutputDevices: MediaDeviceInfo[] = [];
	let audioDevicesLoading = false;
	let audioDeviceStatus: 'ready' | 'permission' | 'empty' | 'unavailable' | 'error' = 'ready';
	const supportsOutputDeviceSelection =
		typeof HTMLMediaElement !== 'undefined' && 'setSinkId' in HTMLMediaElement.prototype;

	const refreshAudioDevices = async (requestPermission = false, reportError = false) => {
		const mediaDevices = navigator.mediaDevices;
		if (!mediaDevices?.enumerateDevices) {
			audioDeviceStatus = 'unavailable';
			return;
		}

		audioDevicesLoading = true;
		let permissionError: unknown = null;
		try {
			if (requestPermission) {
				try {
					const stream = await mediaDevices.getUserMedia({ audio: true });
					stream.getTracks().forEach((track) => track.stop());
				} catch (error) {
					permissionError = error;
				}
			}

			const groupedDevices = groupAudioDevices(await mediaDevices.enumerateDevices());
			audioInputDevices = groupedDevices.inputs;
			audioOutputDevices = groupedDevices.outputs;
			audioInputDeviceId = reconcileAudioDeviceId(audioInputDeviceId, audioInputDevices);
			audioOutputDeviceId = reconcileAudioDeviceId(audioOutputDeviceId, audioOutputDevices);

			if (audioInputDevices.length === 0 && audioOutputDevices.length === 0) {
				audioDeviceStatus = permissionError ? 'permission' : 'empty';
			} else {
				audioDeviceStatus = hasAnonymousAudioDevices(groupedDevices) ? 'permission' : 'ready';
			}

			if (permissionError && reportError) {
				toast.error($i18n.t('Permission denied when accessing microphone'));
			}
		} catch (error) {
			console.error('Error enumerating audio devices', error);
			audioDeviceStatus = 'error';
			if (reportError) toast.error($i18n.t('Unable to access audio devices'));
		} finally {
			audioDevicesLoading = false;
		}
	};

	const requestAudioDeviceLabels = () => refreshAudioDevices(true, true);
	const handleAudioDeviceChange = () => void refreshAudioDevices();

	const getVoices = async () => {
		if (($settings?.audio?.tts?.engine ?? '') !== '') {
			const res = await _getVoices(localStorage.token).catch((e) => toast.error(`${e}`));
			voices = res?.voices ?? [];
			return;
		}

		const getVoicesLoop = setInterval(async () => {
			voices = await speechSynthesis.getVoices();
			if (voices.length > 0) clearInterval(getVoicesLoop);
		}, 100);
	};

	const toggleResponseAutoPlayback = async () => {
		responseAutoPlayback = !responseAutoPlayback;
		saveSettings({ responseAutoPlayback });
	};

	const toggleSpeechAutoSend = async () => {
		speechAutoSend = !speechAutoSend;
		saveSettings({ speechAutoSend });
	};

	onMount(() => {
		playbackRate = $settings.audio?.tts?.playbackRate ?? 1;
		speechAutoSend = $settings.speechAutoSend ?? false;
		responseAutoPlayback = $settings.responseAutoPlayback ?? false;
		STTEngine = $settings?.audio?.stt?.engine ?? '';
		STTLanguage = $settings?.audio?.stt?.language ?? '';
		TTSEngine = $settings?.audio?.tts?.engine ?? '';
		TTSEngineConfig = {
			dtype: $settings?.audio?.tts?.config?.dtype ?? ''
		};
		audioInputDeviceId = $settings?.audio?.inputDeviceId ?? '';
		audioOutputDeviceId = $settings?.audio?.outputDeviceId ?? '';
		simultaneousMode = $settings?.audio?.simultaneous?.mode ?? 'off';

		if ($settings?.audio?.tts?.defaultVoice === $config.audio.tts.voice) {
			voice = $settings?.audio?.tts?.voice ?? $config.audio.tts.voice ?? '';
		} else {
			voice = $config.audio.tts.voice ?? '';
		}

		nonLocalVoices = $settings.audio?.tts?.nonLocalVoices ?? false;

		const initializeAudio = async () => {
			await refreshAudioDevices();
			if (audioDeviceStatus === 'permission' || audioDeviceStatus === 'empty') {
				await refreshAudioDevices(true);
			}
			await getVoices();
		};

		navigator.mediaDevices?.addEventListener('devicechange', handleAudioDeviceChange);
		void initializeAudio();

		return () => {
			navigator.mediaDevices?.removeEventListener('devicechange', handleAudioDeviceChange);
		};
	});
</script>

<form
	id="tab-audio"
	class="flex flex-col h-full justify-between space-y-3 text-sm"
	on:submit|preventDefault={async () => {
		saveSettings({
			audio: {
				inputDeviceId: audioInputDeviceId || undefined,
				outputDeviceId: audioOutputDeviceId || undefined,
				simultaneous: {
					mode: simultaneousMode
				},
				stt: {
					engine: STTEngine !== '' ? STTEngine : undefined,
					language: STTLanguage !== '' ? STTLanguage : undefined
				},
				tts: {
					engine: TTSEngine !== '' ? TTSEngine : undefined,
					config: TTSEngine === 'browser-kokoro' ? TTSEngineConfig : undefined,
					playbackRate,
					voice: voice !== '' ? voice : undefined,
					defaultVoice: $config?.audio?.tts?.voice ?? '',
					nonLocalVoices: $config.audio.tts.engine === '' ? nonLocalVoices : undefined
				}
			}
		});
		dispatch('save');
	}}
>
	<div class="space-y-3 overflow-y-scroll max-h-[28rem] md:max-h-full">
		<div>
			<div class="mb-1 text-sm font-medium">{$i18n.t('Audio Devices')}</div>

			<div class="py-0.5 flex w-full justify-between gap-3">
				<div class="self-center text-xs font-medium">{$i18n.t('Microphone')}</div>
				<select
					class="min-w-0 w-56 rounded-md border border-gray-200 bg-transparent px-2 py-1.5 pr-8 text-right text-xs outline-hidden dark:border-gray-700"
					bind:value={audioInputDeviceId}
					aria-label={$i18n.t('Microphone')}
					on:focus={() => refreshAudioDevices()}
					disabled={audioDevicesLoading || audioDeviceStatus === 'unavailable'}
				>
					<option value="">{$i18n.t('Default')}</option>
					{#each audioInputDevices as device}
						<option value={device.deviceId}
							>{device.label ||
								$i18n.t('Microphone {{number}}', {
									number: audioInputDevices.indexOf(device) + 1
								})}</option
						>
					{/each}
				</select>
			</div>

			<div class="py-0.5 flex w-full justify-between gap-3">
				<div class="self-center text-xs font-medium">{$i18n.t('Speaker')}</div>
				<select
					class="min-w-0 w-56 rounded-md border border-gray-200 bg-transparent px-2 py-1.5 pr-8 text-right text-xs outline-hidden dark:border-gray-700"
					bind:value={audioOutputDeviceId}
					aria-label={$i18n.t('Speaker')}
					on:focus={() => refreshAudioDevices()}
					disabled={!supportsOutputDeviceSelection ||
						audioDevicesLoading ||
						audioDeviceStatus === 'unavailable'}
				>
					<option value="">{$i18n.t('Default')}</option>
					{#each audioOutputDevices as device}
						<option value={device.deviceId}
							>{device.label ||
								$i18n.t('Speaker {{number}}', {
									number: audioOutputDevices.indexOf(device) + 1
								})}</option
						>
					{/each}
				</select>
			</div>

			<button
				class="text-xs opacity-60 transition hover:opacity-90 disabled:cursor-wait disabled:opacity-30"
				type="button"
				on:click={requestAudioDeviceLabels}
				disabled={audioDevicesLoading || audioDeviceStatus === 'unavailable'}
			>
				{$i18n.t(audioDevicesLoading ? 'Refreshing audio devices...' : 'Refresh audio devices')}
			</button>

			{#if audioDeviceStatus !== 'ready'}
				<p class="mt-1 text-[11px] leading-4 text-gray-500 dark:text-gray-400" aria-live="polite">
					{#if audioDeviceStatus === 'permission'}
						{$i18n.t('Allow microphone access to display audio device names.')}
					{:else if audioDeviceStatus === 'empty'}
						{$i18n.t('No audio devices found. Check the system settings and reconnect the device.')}
					{:else if audioDeviceStatus === 'unavailable'}
						{$i18n.t('Audio device access requires HTTPS or localhost.')}
					{:else}
						{$i18n.t('Unable to access audio devices')}
					{/if}
				</p>
			{/if}
		</div>

		<div>
			<div class="mb-1 text-sm font-medium">{$i18n.t('Simultaneous Voice Translation')}</div>

			<div class="py-0.5 flex w-full justify-between gap-3">
				<div class="self-center text-xs font-medium">{$i18n.t('Mode')}</div>
				<select
					class="min-w-0 w-56 pr-8 rounded-sm px-2 p-1 text-xs bg-transparent outline-hidden text-right"
					bind:value={simultaneousMode}
					aria-label={$i18n.t('Simultaneous Voice Translation')}
				>
					<option value="off">{$i18n.t('Off')}</option>
					<option value="face_to_face">{$i18n.t('Face-to-face Interpretation')}</option>
					<option value="call">{$i18n.t('Call Interpretation')}</option>
				</select>
			</div>

			<div class="text-xs opacity-60 leading-5">
				{#if simultaneousMode === 'call'}
					{$i18n.t(
						'For online calls, set Speaker to Voicemeeter, VB-CABLE, BlackHole, or another virtual audio device, then select that device as the microphone in your meeting app.'
					)}
				{:else if simultaneousMode === 'face_to_face'}
					{$i18n.t(
						'Face-to-face mode automatically speaks every translated result through the selected speaker.'
					)}
				{:else}
					{$i18n.t('Off mode does not automatically play translated results with TTS.')}
				{/if}
			</div>
		</div>

		<div>
			<div class="mb-1 text-sm font-medium">{$i18n.t('STT Settings')}</div>

			{#if $config.audio.stt.engine !== 'web'}
				<div class="py-0.5 flex w-full justify-between">
					<div class="self-center text-xs font-medium">{$i18n.t('Speech-to-Text Engine')}</div>
					<select
						class="w-fit pr-8 rounded-sm px-2 p-1 text-xs bg-transparent outline-hidden text-right"
						bind:value={STTEngine}
						aria-label={$i18n.t('Speech-to-Text Engine')}
					>
						<option value="">{$i18n.t('Default')}</option>
						<option value="web">{$i18n.t('Web API')}</option>
						<option value="multimodal">{$i18n.t('Multimodal')}</option>
					</select>
				</div>

				<div class="py-0.5 flex w-full justify-between">
					<div class="self-center text-xs font-medium">{$i18n.t('Language')}</div>
					<div class="flex items-center relative text-xs px-3">
						<Tooltip
							content={$i18n.t(
								'The language of the input audio. Supplying the input language in ISO-639-1 (e.g. en) format will improve accuracy and latency. Leave blank to automatically detect the language.'
							)}
							placement="top"
						>
							<input
								type="text"
								bind:value={STTLanguage}
								aria-label={$i18n.t('Speech-to-Text Language')}
								placeholder={$i18n.t('e.g. en')}
								class="text-sm text-right bg-transparent dark:text-gray-300 outline-hidden"
							/>
						</Tooltip>
					</div>
				</div>
			{/if}

			<div class="py-0.5 flex w-full justify-between">
				<div class="self-center text-xs font-medium">
					{$i18n.t('Instant Auto-Send After Voice Transcription')}
				</div>
				<button
					class="p-1 px-3 text-xs flex rounded-sm transition"
					on:click={toggleSpeechAutoSend}
					type="button"
					role="switch"
					aria-checked={speechAutoSend}
				>
					<span class="ml-2 self-center">{$i18n.t(speechAutoSend ? 'On' : 'Off')}</span>
				</button>
			</div>
		</div>

		<div>
			<div class="mb-1 text-sm font-medium">{$i18n.t('TTS Settings')}</div>

			<div class="py-0.5 flex w-full justify-between">
				<div class="self-center text-xs font-medium">{$i18n.t('Text-to-Speech Engine')}</div>
				<select
					class="w-fit pr-8 rounded-sm px-2 p-1 text-xs bg-transparent outline-hidden text-right"
					bind:value={TTSEngine}
					aria-label={$i18n.t('Text-to-Speech Engine')}
				>
					<option value="">{$i18n.t('Default')}</option>
				</select>
			</div>

			{#if TTSEngine === 'browser-kokoro'}
				<div class=" py-0.5 flex w-full justify-between">
					<div class=" self-center text-xs font-medium">{$i18n.t('Kokoro.js Dtype')}</div>
					<div class="flex items-center relative">
						<select
							class="w-fit pr-8 rounded-sm px-2 p-1 text-xs bg-transparent outline-hidden text-right"
							bind:value={TTSEngineConfig.dtype}
							aria-label={$i18n.t('Kokoro.js Dtype')}
							placeholder={$i18n.t('Select dtype')}
						>
							<option value="" disabled selected>{$i18n.t('Select dtype')}</option>
							<option value="fp32">fp32</option>
							<option value="fp16">fp16</option>
							<option value="q8">q8</option>
							<option value="q4">q4</option>
						</select>
					</div>
				</div>
			{/if}

			<div class=" py-0.5 flex w-full justify-between">
				<div class=" self-center text-xs font-medium">{$i18n.t('Auto-Playback Response')}</div>

				<button
					class="p-1 px-3 text-xs flex rounded-sm transition"
					on:click={toggleResponseAutoPlayback}
					type="button"
					role="switch"
					aria-checked={responseAutoPlayback}
				>
					<span class="ml-2 self-center">{$i18n.t(responseAutoPlayback ? 'On' : 'Off')}</span>
				</button>
			</div>

			<div class="py-0.5 flex w-full justify-between">
				<div class="self-center text-xs font-medium">{$i18n.t('Speech Playback Speed')}</div>
				<div class="flex items-center relative text-xs px-3">
					<input
						type="number"
						min="0"
						step="0.01"
						bind:value={playbackRate}
						aria-label={$i18n.t('Speech Playback Speed')}
						class="text-sm text-right bg-transparent dark:text-gray-300 outline-hidden"
					/>
					x
				</div>
			</div>
		</div>

		<hr class="border-gray-100/30 dark:border-gray-850/30" />

		{#if $config.audio.tts.engine === ''}
			<div>
				<div class="mb-2.5 text-sm font-medium">{$i18n.t('Set Voice')}</div>
				<select
					class="w-full text-sm bg-transparent dark:text-gray-300 outline-hidden"
					bind:value={voice}
					aria-label={$i18n.t('Voice')}
				>
					<option value="">{$i18n.t('Default')}</option>
					{#each voices.filter((v) => nonLocalVoices || v.localService === true) as _voice}
						<option value={_voice.name}>{_voice.name}</option>
					{/each}
				</select>
				<div class="flex items-center justify-between my-1.5">
					<div class="text-xs">{$i18n.t('Allow non-local voices')}</div>
					<div class="mt-1">
						<Switch bind:state={nonLocalVoices} />
					</div>
				</div>
			</div>
		{:else}
			<div>
				<div class="mb-2.5 text-sm font-medium">{$i18n.t('Set Voice')}</div>
				<input
					list="voice-list"
					class="w-full text-sm bg-transparent dark:text-gray-300 outline-hidden"
					bind:value={voice}
					aria-label={$i18n.t('Voice')}
					placeholder={$i18n.t('Select a voice')}
				/>
				<datalist id="voice-list">
					{#each voices as voice}
						<option value={voice.id}>{voice.name}</option>
					{/each}
				</datalist>
			</div>
		{/if}
	</div>

	<div class="flex justify-end text-sm font-medium">
		<button
			class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full"
			type="submit"
		>
			{$i18n.t('Save')}
		</button>
	</div>
</form>
