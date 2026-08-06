declare module '@joplin/turndown-plugin-gfm' {
	import type TurndownService from 'turndown';

	export const gfm: (service: TurndownService) => void;
}

declare module '@sveltejs/svelte-virtual-list' {
	const VirtualList: any;
	export default VirtualList;
}

declare module 'katex/contrib/mhchem';

interface SaveFilePickerOptions {
	excludeAcceptAllOption?: boolean;
	suggestedName?: string;
	types?: {
		description?: string;
		accept: Record<string, string[]>;
	}[];
}

interface Window {
	showSaveFilePicker(options?: SaveFilePickerOptions): Promise<FileSystemFileHandle>;
}
