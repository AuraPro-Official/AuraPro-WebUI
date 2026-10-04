"""Refresh a curated, offline language catalog from Google's public language menu.

After updating, format the generated JSON with the repository's Prettier command.
"""

import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import urlopen


# Broad coverage without newly added, uncommon regional or script variants.
COMMON_LANGUAGE_CODES = set(
    'af sq am ar hy as ay az bm eu be bn bho bs bg ca ceb ny zh-CN zh-TW '
    'co hr cs da dv doi nl en eo et ee fi fr fy gl ka de el gn gu ht ha haw iw '
    'hi hmn hu is ig ilo id ga it ja jw kn kk km rw gom ko kri ku ckb ky lo '
    'la lv ln lt lg lb mk mai mg ms ml mt mi mr mni-Mtei lus mn my ne no '
    'or om ps fa pl pt pa qu ro ru sm sa gd nso sr st sn sd si sk sl so es su '
    'sw sv tg ta tt te th ti ts tr tk ak uk ur ug uz vi cy xh yi yo zu tl bo yue'.split()
)


class LanguageMenuParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.languages = {}
        self.depth = 0
        self.option = None
        self.label_depth = None
        self.label = []

    def handle_starttag(self, tag, attrs):
        if tag != 'div':
            return
        self.depth += 1
        attrs = dict(attrs)
        code = attrs.get('data-language-code')
        if code and code != 'auto' and attrs.get('role') == 'option':
            self.option = (self.depth, code)
        if self.option and 'Llmcnf' in attrs.get('class', '').split():
            self.label_depth = self.depth
            self.label = []

    def handle_data(self, data):
        if self.label_depth is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag != 'div':
            return
        if self.label_depth == self.depth:
            name = ''.join(self.label).strip()
            if name:
                self.languages[self.option[1]] = name
            self.label_depth = None
        if self.option and self.option[0] == self.depth:
            self.option = None
        self.depth -= 1


def main():
    menus = {}
    for locale in ('en', 'zh-CN', 'zh-TW', 'es', 'fr'):
        with urlopen(f'https://translate.google.com/?hl={locale}', timeout=60) as response:
            parser = LanguageMenuParser()
            parser.feed(response.read().decode('utf-8'))
        if len(parser.languages) < 240:
            raise RuntimeError(f'Incomplete language menu for {locale}: {len(parser.languages)}')
        menus[locale] = parser.languages
    codes = set(menus['en'])
    if any(set(menu) != codes for menu in menus.values()):
        raise RuntimeError('Localized language menus differ; review before updating the catalog')
    missing = COMMON_LANGUAGE_CODES - codes
    if missing:
        raise RuntimeError(f'Missing curated languages: {sorted(missing)}')
    languages = {
        code: {
            'name': menus['en'][code],
            'aliases': sorted({menu[code] for menu in menus.values()}),
        }
        for code in sorted(COMMON_LANGUAGE_CODES)
    }
    languages['pt'] = {
        'name': 'Portuguese',
        'aliases': ['Portuguese', 'Portugais', 'portugués', '葡萄牙文', '葡萄牙语'],
    }
    catalog = {'source': 'https://translate.google.com/', 'languages': languages}
    output = Path(__file__).resolve().parents[1] / 'backend/open_webui/utils/chat_translation_languages.json'
    output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Updated {len(languages)} language entries: {output}')


if __name__ == '__main__':
    main()
