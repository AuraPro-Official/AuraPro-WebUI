<script lang="ts">
	import { createEventDispatcher, getContext, onDestroy } from 'svelte';

	import { settings } from '$lib/stores';
	import Tooltip from './Tooltip.svelte';
	import Spinner from './Spinner.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	const i18n = getContext('i18n');
	const dispatch = createEventDispatcher();

	export let src: string;
	export let name: string = '';
	export let dismissible = false;
	export let loading = false;
	export let className = 'w-60';

	let audio: HTMLAudioElement | null = null;
	let playing = false;
	let duration = 0;
	let currentTime = 0;

	const formatTime = (seconds: number) => {
		if (!isFinite(seconds) || isNaN(seconds) || seconds < 0) {
			return '0:00';
		}
		const m = Math.floor(seconds / 60);
		const s = Math.floor(seconds % 60);
		return `${m}:${s < 10 ? '0' : ''}${s}`;
	};

	const resolveDuration = () => {
		if (!audio) return;
		// Chrome reports Infinity for MediaRecorder blobs that lack duration
		// metadata; seeking far past the end forces the real duration to resolve.
		if (audio.duration === Infinity) {
			const onSeeked = () => {
				if (!audio) return;
				duration = audio.duration;
				audio.removeEventListener('seeked', onSeeked);
				audio.currentTime = 0;
			};
			audio.addEventListener('seeked', onSeeked);
			audio.currentTime = 1e10;
		} else {
			duration = audio.duration;
		}
	};

	const togglePlay = () => {
		if (!audio) return;
		if (playing) {
			audio.pause();
		} else {
			audio.play();
		}
	};

	const seek = (event: MouseEvent) => {
		if (!audio || !isFinite(duration) || duration <= 0) return;
		const track = event.currentTarget as HTMLElement;
		const rect = track.getBoundingClientRect();
		const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
		audio.currentTime = ratio * duration;
		currentTime = audio.currentTime;
	};

	$: progress = isFinite(duration) && duration > 0 ? Math.min(1, currentTime / duration) : 0;

	onDestroy(() => {
		if (audio) {
			audio.pause();
		}
	});
</script>

<div
	class="relative group p-2 {className} flex items-center gap-2.5 bg-white dark:bg-gray-850 border border-gray-50/30 dark:border-gray-800/30 rounded-2xl"
>
	<audio
		bind:this={audio}
		{src}
		preload="metadata"
		on:loadedmetadata={resolveDuration}
		on:timeupdate={() => {
			if (audio) currentTime = audio.currentTime;
		}}
		on:durationchange={() => {
			if (audio && isFinite(audio.duration)) duration = audio.duration;
		}}
		on:play={() => (playing = true)}
		on:pause={() => (playing = false)}
		on:ended={() => {
			playing = false;
			currentTime = 0;
		}}
	/>

	<button
		class="size-9 shrink-0 flex justify-center items-center rounded-full bg-indigo-500 hover:bg-indigo-600 text-white transition"
		type="button"
		aria-label={playing ? $i18n.t('Pause') : $i18n.t('Play')}
		disabled={loading}
		on:click={togglePlay}
	>
		{#if loading}
			<Spinner className="size-4" />
		{:else if playing}
			<svg
				xmlns="http://www.w3.org/2000/svg"
				viewBox="0 0 24 24"
				fill="currentColor"
				class="size-4"
			>
				<path
					fill-rule="evenodd"
					d="M6.75 5.25a.75.75 0 0 1 .75-.75H9a.75.75 0 0 1 .75.75v13.5a.75.75 0 0 1-.75.75H7.5a.75.75 0 0 1-.75-.75V5.25Zm7.5 0A.75.75 0 0 1 15 4.5h1.5a.75.75 0 0 1 .75.75v13.5a.75.75 0 0 1-.75.75H15a.75.75 0 0 1-.75-.75V5.25Z"
					clip-rule="evenodd"
				/>
			</svg>
		{:else}
			<svg
				xmlns="http://www.w3.org/2000/svg"
				viewBox="0 0 24 24"
				fill="currentColor"
				class="size-4 ml-0.5"
			>
				<path
					fill-rule="evenodd"
					d="M4.5 5.653c0-1.427 1.529-2.33 2.779-1.643l11.54 6.347c1.295.712 1.295 2.573 0 3.286L7.28 19.99c-1.25.687-2.779-.217-2.779-1.643V5.653Z"
					clip-rule="evenodd"
				/>
			</svg>
		{/if}
	</button>

	<div class="flex flex-col justify-center flex-1 min-w-0 gap-1 pr-1">
		<div class="flex items-center gap-1.5">
			<svg
				xmlns="http://www.w3.org/2000/svg"
				viewBox="0 0 24 24"
				fill="currentColor"
				class="size-3 shrink-0 text-indigo-500 dark:text-indigo-400"
			>
				<path d="M8.25 4.5a3.75 3.75 0 1 1 7.5 0v8.25a3.75 3.75 0 1 1-7.5 0V4.5Z" />
				<path
					d="M6 10.5a.75.75 0 0 1 .75.75v1.5a5.25 5.25 0 1 0 10.5 0v-1.5a.75.75 0 0 1 1.5 0v1.5a6.751 6.751 0 0 1-6 6.709v2.291h3a.75.75 0 0 1 0 1.5h-7.5a.75.75 0 0 1 0-1.5h3v-2.291a6.751 6.751 0 0 1-6-6.709v-1.5A.75.75 0 0 1 6 10.5Z"
				/>
			</svg>
			<Tooltip content={name} className="min-w-0" placement="top-start">
				<div class="text-xs font-medium dark:text-gray-100 truncate">
					{$i18n.t('Voice message')}
				</div>
			</Tooltip>
		</div>

		<!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
		<div
			class="h-1.5 w-full rounded-full bg-gray-100 dark:bg-gray-800 cursor-pointer overflow-hidden"
			on:click|stopPropagation={seek}
		>
			<div
				class="h-full rounded-full bg-indigo-500 dark:bg-indigo-400 transition-[width] duration-100"
				style="width: {progress * 100}%;"
			/>
		</div>

		<div
			class="text-[10px] tabular-nums {($settings?.highContrastMode ?? false)
				? 'text-gray-800 dark:text-gray-100'
				: 'text-gray-500'}"
		>
			{formatTime(currentTime)} / {formatTime(duration)}
		</div>
	</div>

	{#if dismissible}
		<div class=" absolute -top-1 -right-1">
			<button
				aria-label={$i18n.t('Remove File')}
				class=" bg-white text-black border border-gray-50 rounded-full {($settings?.highContrastMode ??
				false)
					? ''
					: 'outline-hidden focus:outline-hidden group-hover:visible invisible transition'}"
				type="button"
				on:click|stopPropagation={() => {
					dispatch('dismiss');
				}}
			>
				<XMark className={'size-4'} />
			</button>
		</div>
	{/if}
</div>
