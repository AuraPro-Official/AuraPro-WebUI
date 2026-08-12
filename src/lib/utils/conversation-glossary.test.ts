import { describe, expect, it } from 'vitest';

import {
	getConversationGlossaryLanguages,
	getDefaultConversationGlossary,
	mergeRecentGlossaryLanguages,
	normalizeConversationGlossary,
	type GlossarySettings
} from './conversation-glossary';

const settings: GlossarySettings = {
	glossary_mode: 'smart',
	active_glossary_id: 'zh-es',
	smart_source_lang: 'Chinese',
	smart_target_lang: 'Spanish',
	glossaries: [
		{
			id: 'zh-es',
			name: 'Chinese-Spanish',
			source_lang: 'Chinese',
			target_lang: 'Spanish'
		},
		{
			id: 'en-fr',
			name: 'English-French',
			source_lang: 'English',
			target_lang: 'French'
		}
	]
};

describe('conversation glossary', () => {
	it('inherits the global smart language pair for a new conversation', () => {
		expect(getDefaultConversationGlossary(settings)).toEqual({
			mode: 'smart',
			source_lang: 'Chinese',
			target_lang: 'Spanish'
		});
	});

	it('keeps a known fixed glossary and rejects an unknown id', () => {
		expect(
			normalizeConversationGlossary({ mode: 'fixed', glossary_id: 'en-fr' }, settings)
		).toEqual({ mode: 'fixed', glossary_id: 'en-fr' });
		expect(
			normalizeConversationGlossary({ mode: 'fixed', glossary_id: 'missing' }, settings)
		).toEqual(getDefaultConversationGlossary(settings));
	});

	it('derives speech recognition languages from the current conversation', () => {
		expect(
			getConversationGlossaryLanguages({ mode: 'fixed', glossary_id: 'en-fr' }, settings)
		).toEqual(['English', 'French']);
		expect(
			getConversationGlossaryLanguages(
				{ mode: 'smart', source_lang: 'Spanish', target_lang: 'French' },
				settings
			)
		).toEqual(['Spanish', 'French']);
	});

	it('keeps the ten most recently used languages without duplicates', () => {
		expect(
			mergeRecentGlossaryLanguages(
				['English', 'French', 'German', 'Italian', 'Japanese', 'Korean', 'Portuguese'],
				[' Spanish ', 'english', 'Chinese', 'Dutch']
			)
		).toEqual([
			'Spanish',
			'english',
			'Chinese',
			'Dutch',
			'French',
			'German',
			'Italian',
			'Japanese',
			'Korean',
			'Portuguese'
		]);
		expect(mergeRecentGlossaryLanguages([], ['English'], 0)).toEqual([]);
	});
});
