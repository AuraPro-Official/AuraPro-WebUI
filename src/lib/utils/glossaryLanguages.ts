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
	'bs',
	'ca',
	'cs',
	'cy',
	'da',
	'de',
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
	'la',
	'lb',
	'ln',
	'lo',
	'lt',
	'lv',
	'mg',
	'mi',
	'mk',
	'ml',
	'mn',
	'mr',
	'ms',
	'mt',
	'my',
	'ne',
	'nl',
	'nn',
	'no',
	'oc',
	'pa',
	'pl',
	'ps',
	'pt',
	'ro',
	'ru',
	'sa',
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
	国语: 'zh',
	國語: 'zh',
	华语: 'zh',
	華語: 'zh',
	简体中文: 'zh',
	簡體中文: 'zh',
	繁体中文: 'zh',
	繁體中文: 'zh',
	英文: 'en',
	西语: 'es',
	西語: 'es',
	葡语: 'pt',
	葡語: 'pt',
	广东话: 'yue',
	廣東話: 'yue',
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
	const baseCode = normalized.split('-', 1)[0];
	return LEGACY_CODE_ALIASES[baseCode] ?? baseCode;
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

for (const code of COMMON_LANGUAGE_CODES) {
	registerAlias(code, code);
	registerAlias(code, getDisplayName(chineseDisplayNames, code));
	registerAlias(code, getDisplayName(englishDisplayNames, code));
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

const resolveGlossaryLanguage = (value: string): ResolvedGlossaryLanguage | null => {
	const raw = String(value ?? '')
		.normalize('NFKC')
		.trim();
	if (!raw) return null;

	const normalizedCode = raw.toLocaleLowerCase().replaceAll('_', '-');
	const isLanguageCode = /^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$/.test(normalizedCode);
	const code = isLanguageCode
		? canonicalizeCode(normalizedCode)
		: codeByName.get(normalizeSearchText(raw));

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
