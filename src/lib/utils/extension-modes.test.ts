import { afterEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';

import {
	codeInterpreterEnabled,
	interpretationModeEnabled,
	learningModeEnabled,
	manuscriptTranslationModeEnabled,
	ragTranslationModeEnabled,
	translationModeEnabled
} from '$lib/stores';
import {
	applyDesktopShortcutAction,
	applyExtensionMode,
	normalizeExtensionMode
} from './extension-modes';

const modeStores = {
	translation: translationModeEnabled,
	manuscript_translation: manuscriptTranslationModeEnabled,
	interpretation: interpretationModeEnabled,
	learning: learningModeEnabled,
	rag_translation: ragTranslationModeEnabled
};

afterEach(() => {
	applyExtensionMode('');
	codeInterpreterEnabled.set(false);
});

describe('extension modes', () => {
	it('normalizes persisted values and rejects unknown values', () => {
		expect(normalizeExtensionMode('translation')).toBe('translation');
		expect(normalizeExtensionMode('rag_translation')).toBe('rag_translation');
		expect(normalizeExtensionMode('unknown')).toBe('');
		expect(normalizeExtensionMode(null)).toBe('');
	});

	it.each(Object.keys(modeStores) as (keyof typeof modeStores)[])(
		'enables only the selected %s mode',
		(mode) => {
			applyExtensionMode(mode);

			for (const [candidate, store] of Object.entries(modeStores)) {
				expect(get(store), candidate).toBe(candidate === mode);
			}
		}
	);

	it('maps the desktop simultaneous action to interpretation mode', () => {
		applyDesktopShortcutAction('simultaneous');

		expect(get(interpretationModeEnabled)).toBe(true);
		expect(get(translationModeEnabled)).toBe(false);
	});

	it('enables the code interpreter desktop action', () => {
		applyDesktopShortcutAction('code_interpreter');
		expect(get(codeInterpreterEnabled)).toBe(true);
	});
});
