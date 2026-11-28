import translationLanguageCatalog from '../../../backend/open_webui/utils/chat_translation_languages.json' with { type: 'json' };

export type GlossaryLanguageSelectItem = {
	value: string;
	label: string;
	searchTerms: string[];
};

const COMMON_LANGUAGE_CODES = [
	'af',
	'am',
	'ar',
	'as',
	'az',
	'ba',
	'be',
	'bg',
	'bn',
	'bo',
	'br',
	'brx',
	'bs',
	'ca',
	'cs',
	'cy',
	'da',
	'de',
	'doi',
	'el',
	'en',
	'es',
	'et',
	'eu',
	'fa',
	'fi',
	'fil',
	'fo',
	'fr',
	'gl',
	'gu',
	'ha',
	'haw',
	'he',
	'hi',
	'hr',
	'ht',
	'hu',
	'hy',
	'id',
	'is',
	'it',
	'ja',
	'jv',
	'ka',
	'kk',
	'km',
	'kn',
	'ko',
	'kok',
	'ks',
	'ku',
	'la',
	'lb',
	'ln',
	'lo',
	'lt',
	'lv',
	'mai',
	'mg',
	'mi',
	'mk',
	'ml',
	'mn',
	'mni',
	'mr',
	'ms',
	'mt',
	'my',
	'nan',
	'ne',
	'nl',
	'nn',
	'no',
	'oc',
	'or',
	'pa',
	'pl',
	'ps',
	'pt',
	'ro',
	'ru',
	'sa',
	'sat',
	'sd',
	'si',
	'sk',
	'sl',
	'sn',
	'so',
	'sq',
	'sr',
	'su',
	'sv',
	'sw',
	'ta',
	'te',
	'tg',
	'th',
	'tk',
	'tlh',
	'tn',
	'tr',
	'tt',
	'uk',
	'ur',
	'uz',
	'vi',
	'yi',
	'yo',
	'yue',
	'zh'
] as const;

const LEGACY_CODE_ALIASES: Record<string, string> = {
	cmn: 'zh',
	in: 'id',
	iw: 'he',
	jw: 'jv',
	tl: 'fil'
};

const MANUAL_NAME_ALIASES: Record<string, string> = {
	中文: 'zh',
	汉语: 'zh',
	漢語: 'zh',
	普通话: 'zh',
	普通話: 'zh',
	mandarin: 'zh',
	国语: 'zh',
	國語: 'zh',
	华语: 'zh',
	華語: 'zh',
	简体中文: 'zh',
	簡體中文: 'zh',
	繁体中文: 'zh',
	繁體中文: 'zh',
	英文: 'en',
	英語: 'en',
	尼泊爾語: 'ne',
	印地語: 'hi',
	孟加拉語: 'bn',
	bengali: 'bn',
	菲律賓語: 'fil',
	tagalog: 'fil',
	西语: 'es',
	西語: 'es',
	西班牙語: 'es',
	葡语: 'pt',
	葡語: 'pt',
	广东话: 'yue',
	廣東話: 'yue',
	闽南语: 'nan',
	閩南語: 'nan',
	'min nan': 'nan',
	塔加洛语: 'fil',
	塔加洛語: 'fil',
	他加禄语: 'fil',
	他加祿語: 'fil'
};

const normalizeSearchText = (value: string): string =>
	value
		.normalize('NFKC')
		.toLocaleLowerCase()
		.replace(/[\s._/\\\-()（）]+/g, '')
		.trim();

const createDisplayNames = (locale: string): Intl.DisplayNames | null => {
	try {
		return new Intl.DisplayNames([locale], { type: 'language' });
	} catch {
		return null;
	}
};

const chineseDisplayNames = createDisplayNames('zh-CN');
const englishDisplayNames = createDisplayNames('en');
const additionalDisplayNames = ['zh-TW', 'es'].map(createDisplayNames);

const getDisplayName = (displayNames: Intl.DisplayNames | null, code: string): string | null => {
	try {
		const label = displayNames?.of(code)?.trim();
		return label && label.toLocaleLowerCase() !== code.toLocaleLowerCase() ? label : null;
	} catch {
		return null;
	}
};

const canonicalizeCode = (code: string): string => {
	const normalized = code.normalize('NFKC').trim().toLocaleLowerCase().replaceAll('_', '-');
	if (normalized === 'zh-yue') return 'yue';
	if (normalized === 'zh-min-nan') return 'nan';
	const base = normalized.split('-', 1)[0];
	if (Object.hasOwn(LEGACY_CODE_ALIASES, base)) return LEGACY_CODE_ALIASES[base];
	try {
		return Intl.getCanonicalLocales(normalized)[0]?.split('-', 1)[0] ?? base;
	} catch {
		return base;
	}
};

const aliasesByCode = new Map<string, Set<string>>();
const codeByName = new Map<string, string>();

const registerAlias = (code: string, alias: string | null | undefined) => {
	const value = alias?.trim();
	if (!value) return;

	const canonicalCode = canonicalizeCode(code);
	codeByName.set(normalizeSearchText(value), canonicalCode);
	const aliases = aliasesByCode.get(canonicalCode) ?? new Set<string>();
	aliases.add(value);
	aliasesByCode.set(canonicalCode, aliases);
};

for (const code of new Set([
	...COMMON_LANGUAGE_CODES,
	...Object.keys(translationLanguageCatalog.languages)
])) {
	registerAlias(code, code);
	registerAlias(code, getDisplayName(chineseDisplayNames, code));
	registerAlias(code, getDisplayName(englishDisplayNames, code));
	for (const displayNames of additionalDisplayNames) {
		registerAlias(code, getDisplayName(displayNames, code));
	}
}

for (const [code, language] of Object.entries(translationLanguageCatalog.languages)) {
	for (const name of [language.name, ...language.aliases]) registerAlias(code, name);
}

for (const [alias, code] of Object.entries(LEGACY_CODE_ALIASES)) {
	registerAlias(code, alias);
}

for (const [alias, code] of Object.entries(MANUAL_NAME_ALIASES)) {
	registerAlias(code, alias);
}

type ResolvedGlossaryLanguage = {
	key: string;
	label: string;
	searchTerms: string[];
};

export const glossaryLanguageCode = (value: string): string | undefined => {
	const raw = String(value ?? '')
		.normalize('NFKC')
		.trim();
	if (!raw) return undefined;

	const normalizedCode = raw.toLocaleLowerCase().replaceAll('_', '-');
	const isLanguageCode = /^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$/.test(normalizedCode);
	return isLanguageCode
		? canonicalizeCode(normalizedCode)
		: codeByName.get(normalizeSearchText(raw));
};

export const glossarySpeechLanguageCode = (value: string): string | undefined => {
	const code = glossaryLanguageCode(value);
	if (code === 'und') return undefined;
	return code === 'fil' ? 'tl' : code;
};

const resolveGlossaryLanguage = (value: string): ResolvedGlossaryLanguage | null => {
	const raw = String(value ?? '')
		.normalize('NFKC')
		.trim();
	if (!raw) return null;
	const code = glossaryLanguageCode(raw);

	if (!code) {
		return {
			key: `custom:${normalizeSearchText(raw)}`,
			label: raw,
			searchTerms: [raw]
		};
	}

	const label = getDisplayName(chineseDisplayNames, code) ?? raw;
	const searchTerms = new Set<string>([
		raw,
		code,
		label,
		getDisplayName(englishDisplayNames, code) ?? '',
		...(aliasesByCode.get(code) ?? [])
	]);

	return {
		key: `code:${code}`,
		label,
		searchTerms: Array.from(searchTerms).filter(Boolean)
	};
};

export const normalizeGlossaryLanguage = (value: string): string =>
	resolveGlossaryLanguage(value)?.label ?? '';

export const buildGlossaryLanguageSelectItems = (
	values: Array<string | null | undefined>
): GlossaryLanguageSelectItem[] => {
	const grouped = new Map<string, { label: string; searchTerms: Set<string> }>();

	for (const value of values) {
		const resolved = resolveGlossaryLanguage(value ?? '');
		if (!resolved) continue;

		const existing = grouped.get(resolved.key);
		if (existing) {
			for (const term of resolved.searchTerms) existing.searchTerms.add(term);
			continue;
		}

		grouped.set(resolved.key, {
			label: resolved.label,
			searchTerms: new Set(resolved.searchTerms)
		});
	}

	return Array.from(grouped.values())
		.map(({ label, searchTerms }) => ({
			value: label,
			label,
			searchTerms: Array.from(searchTerms)
		}))
		.sort((left, right) => left.label.localeCompare(right.label, 'zh-CN'));
};
