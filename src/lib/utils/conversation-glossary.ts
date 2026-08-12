export type GlossaryOption = {
	id: string;
	name: string;
	source_lang?: string;
	glossary_lang?: string;
	target_lang?: string;
};

export type GlossarySettings = {
	glossary_mode?: 'smart' | 'fixed';
	active_glossary_id?: string;
	smart_source_lang?: string;
	smart_target_lang?: string;
	source_lang?: string;
	glossary_lang?: string;
	target_lang?: string;
	glossaries?: GlossaryOption[];
};

export type ConversationGlossaryConfig = {
	mode: 'smart' | 'fixed';
	glossary_id?: string;
	source_lang?: string;
	target_lang?: string;
};

const cleanLanguage = (value: unknown, fallback: string) => {
	const language = typeof value === 'string' ? value.trim().replace(/\s+/g, ' ') : '';
	return language && language.length <= 80 ? language : fallback;
};

const getSmartLanguages = (settings: GlossarySettings | null | undefined) => ({
	source: settings?.smart_source_lang || settings?.source_lang || '',
	target: settings?.smart_target_lang || settings?.target_lang || settings?.glossary_lang || ''
});

export const getDefaultConversationGlossary = (
	settings: GlossarySettings | null | undefined
): ConversationGlossaryConfig | null => {
	if (!settings) return null;

	if (settings.glossary_mode === 'fixed') {
		const active = settings.glossaries?.find(
			(glossary) => glossary.id === settings.active_glossary_id
		);
		if (active) {
			return { mode: 'fixed', glossary_id: active.id };
		}
	}

	const languages = getSmartLanguages(settings);
	return {
		mode: 'smart',
		source_lang: languages.source,
		target_lang: languages.target
	};
};

export const normalizeConversationGlossary = (
	value: unknown,
	settings: GlossarySettings | null | undefined
): ConversationGlossaryConfig | null => {
	const fallback = getDefaultConversationGlossary(settings);
	if (!settings || !value || typeof value !== 'object') return fallback;

	const candidate = value as Record<string, unknown>;
	if (candidate.mode === 'fixed') {
		const glossaryId = typeof candidate.glossary_id === 'string' ? candidate.glossary_id : '';
		if (settings.glossaries?.some((glossary) => glossary.id === glossaryId)) {
			return { mode: 'fixed', glossary_id: glossaryId };
		}
		return fallback;
	}

	if (candidate.mode === 'smart') {
		const languages = getSmartLanguages(settings);
		return {
			mode: 'smart',
			source_lang: cleanLanguage(candidate.source_lang, languages.source),
			target_lang: cleanLanguage(candidate.target_lang, languages.target)
		};
	}

	return fallback;
};

export const getConversationGlossaryLanguages = (
	value: ConversationGlossaryConfig | null | undefined,
	settings: GlossarySettings | null | undefined
) => {
	const normalized = normalizeConversationGlossary(value, settings);
	if (normalized?.mode === 'fixed') {
		const glossary = settings?.glossaries?.find((item) => item.id === normalized.glossary_id);
		return [
			glossary?.source_lang || '',
			glossary?.target_lang || glossary?.glossary_lang || ''
		].filter(Boolean);
	}

	return [normalized?.source_lang || '', normalized?.target_lang || ''].filter(Boolean);
};

export const getGlossaryLanguages = (settings: GlossarySettings | null | undefined) =>
	Array.from(
		new Set(
			(settings?.glossaries ?? []).flatMap((glossary) => [
				glossary.source_lang,
				glossary.target_lang || glossary.glossary_lang
			])
		)
	)
		.filter((language): language is string => Boolean(language))
		.sort((a, b) => a.localeCompare(b));
