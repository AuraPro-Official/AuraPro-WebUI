import {
	interpretationModeEnabled,
	codeInterpreterEnabled,
	learningModeEnabled,
	manuscriptTranslationModeEnabled,
	openCodeModeEnabled,
	ragTranslationModeEnabled,
	translationModeEnabled
} from '$lib/stores';

export const EXTENSION_MODE_VALUES = [
	'',
	'translation',
	'manuscript_translation',
	'interpretation',
	'learning',
	'rag_translation'
] as const;

export type ExtensionMode = (typeof EXTENSION_MODE_VALUES)[number];

export const normalizeExtensionMode = (value: unknown): ExtensionMode =>
	EXTENSION_MODE_VALUES.includes(value as ExtensionMode) ? (value as ExtensionMode) : '';

const ACTIVE_EXTENSION_MODE_VALUES = [...EXTENSION_MODE_VALUES, 'code'] as const;
type ActiveExtensionMode = (typeof ACTIVE_EXTENSION_MODE_VALUES)[number];

export const applyExtensionMode = (value: unknown): void => {
	const mode: ActiveExtensionMode = ACTIVE_EXTENSION_MODE_VALUES.includes(
		value as ActiveExtensionMode
	)
		? (value as ActiveExtensionMode)
		: '';

	translationModeEnabled.set(mode === 'translation');
	manuscriptTranslationModeEnabled.set(mode === 'manuscript_translation');
	interpretationModeEnabled.set(mode === 'interpretation');
	learningModeEnabled.set(mode === 'learning');
	ragTranslationModeEnabled.set(mode === 'rag_translation');
	openCodeModeEnabled.set(mode === 'code');
};

export const applyDesktopShortcutAction = (action: unknown): void => {
	switch (action) {
		case 'translation':
		case 'manuscript_translation':
		case 'learning':
			applyExtensionMode(action);
			break;
		case 'simultaneous':
			applyExtensionMode('interpretation');
			break;
		case 'code_interpreter':
			codeInterpreterEnabled.set(true);
			break;
	}
};
