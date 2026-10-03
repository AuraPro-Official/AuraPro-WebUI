import { describe, expect, it } from 'vitest';
import {
	mergeGlossaryRuntimeSettings,
	type GlossaryRuntimeSettings
} from './glossaryRuntimeSettings';

describe('glossary runtime settings synchronization', () => {
	const saved: GlossaryRuntimeSettings = { token_limit: 16384, kv_cache_type: 'q8_0' };

	it('updates open settings after a desktop change without replacing glossary edits', () => {
		const draft = { ...saved, max_terms_injected: 25 };
		expect(
			mergeGlossaryRuntimeSettings(draft, saved, { token_limit: 32768, kv_cache_type: 'q4_0' })
		).toEqual({ token_limit: 32768, kv_cache_type: 'q4_0', max_terms_injected: 25 });
	});

	it('preserves an unsaved precision selection while syncing context size', () => {
		const draft: GlossaryRuntimeSettings = { ...saved, kv_cache_type: 'f16' };
		expect(
			mergeGlossaryRuntimeSettings(draft, saved, { token_limit: 32768, kv_cache_type: 'q4_0' })
		).toEqual({ token_limit: 32768, kv_cache_type: 'f16' });
	});

	it('preserves an unsaved context size while syncing precision', () => {
		const draft = { ...saved, token_limit: 24576 };
		expect(
			mergeGlossaryRuntimeSettings(draft, saved, { token_limit: 32768, kv_cache_type: 'f16' })
		).toEqual({ token_limit: 24576, kv_cache_type: 'f16' });
	});
});
