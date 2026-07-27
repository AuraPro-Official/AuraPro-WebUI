import type { i18n as I18n } from 'i18next';
import type { Writable } from 'svelte/store';

declare module 'svelte' {
	function getContext(key: 'i18n'): Writable<I18n>;
}

// See https://kit.svelte.dev/docs/types#app
// for information about these interfaces
declare global {
	const APP_VERSION: string;
	const APP_BUILD_HASH: string;

	namespace App {
		// interface Error {}
		// interface Locals {}
		// interface PageData {}
		// interface Platform {}
	}
}

export {};
