from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if 'open_webui' not in sys.modules:
    package = types.ModuleType('open_webui')
    package.__path__ = [str(BACKEND / 'open_webui')]
    sys.modules['open_webui'] = package

from open_webui.utils.glossary_routing import (  # noqa: E402
    GlossaryDataset,
    language_key,
    resolve_smart_glossary,
)


def dataset(
    glossary_id: str,
    source_lang: str,
    target_lang: str,
    entries: dict[str, str],
    *,
    official: bool = True,
) -> GlossaryDataset:
    return GlossaryDataset(
        id=glossary_id,
        name=glossary_id,
        source_lang=source_lang,
        target_lang=target_lang,
        entries=entries,
        official=official,
    )


class TestGlossaryRouting(unittest.TestCase):
    def test_language_aliases_are_canonical(self) -> None:
        self.assertEqual(language_key('西班牙语'), 'es')
        self.assertEqual(language_key('Spanish'), 'es')
        self.assertEqual(language_key('葡语'), 'pt')
        self.assertEqual(language_key('pt-BR'), 'pt-br')

    def test_combines_two_dictionaries_through_chinese(self) -> None:
        result = resolve_smart_glossary(
            [
                dataset(
                    'zh-es',
                    '中文',
                    '西班牙语',
                    {'你好': 'hola', '苹果': 'manzana'},
                ),
                dataset(
                    'zh-pt',
                    '中文',
                    '葡萄牙语',
                    {'你好': 'olá', '苹果': 'maçã'},
                ),
            ],
            '西班牙语',
            '葡萄牙语',
        )

        self.assertEqual(result.entries, {'hola': 'olá', 'manzana': 'maçã'})
        self.assertEqual(result.entry_origins['hola'], 'combined')
        self.assertEqual(result.routes[0].pivot_lang, '中文')

    def test_reverse_combination_is_supported(self) -> None:
        result = resolve_smart_glossary(
            [
                dataset('zh-es', '中文', '西班牙语', {'你好': 'hola'}),
                dataset('zh-pt', '中文', '葡萄牙语', {'你好': 'olá'}),
            ],
            '葡萄牙语',
            '西班牙语',
        )

        self.assertEqual(result.entries, {'olá': 'hola'})

    def test_user_entries_override_direct_and_combined_terms(self) -> None:
        result = resolve_smart_glossary(
            [
                dataset('zh-es', '中文', '西班牙语', {'你好': 'hola'}),
                dataset('zh-pt', '中文', '葡萄牙语', {'你好': 'olá'}),
                dataset(
                    'es-pt',
                    '西班牙语',
                    '葡萄牙语',
                    {'hola': 'olá oficial', 'gracias': 'obrigado'},
                ),
                dataset(
                    'user-es-pt',
                    '西班牙语',
                    '葡萄牙语',
                    {'hola': 'oi'},
                    official=False,
                ),
            ],
            'es',
            'pt',
        )

        self.assertEqual(result.entries['hola'], 'oi')
        self.assertEqual(result.entries['gracias'], 'obrigado')
        self.assertEqual(result.entry_origins['hola'], 'user')
        self.assertEqual(result.entry_origins['gracias'], 'direct')

    def test_same_priority_candidates_are_kept_for_ai_context(self) -> None:
        result = resolve_smart_glossary(
            [
                dataset(
                    'direct-a',
                    '西班牙语',
                    '葡萄牙语',
                    {'banco': 'banco'},
                ),
                dataset(
                    'direct-b',
                    'es',
                    'pt',
                    {'banco': 'margem'},
                ),
            ],
            'Spanish',
            'Portuguese',
        )

        self.assertEqual(result.entries['banco'], 'banco / margem')

    def test_combines_crossed_pairs_through_english_in_both_directions(self) -> None:
        datasets = [
            dataset('zh-en', '中文', '英语', {'你好': 'hello'}),
            dataset('en-fr', '英语', '法语', {'hello': 'bonjour'}),
        ]

        forward = resolve_smart_glossary(datasets, '中文', '法语')
        reverse = resolve_smart_glossary(datasets, '法语', '中文')

        self.assertEqual(forward.entries, {'你好': 'bonjour'})
        self.assertEqual(reverse.entries, {'bonjour': '你好'})
        self.assertEqual(forward.routes[0].pivot_lang, '英语')
        self.assertEqual(reverse.routes[0].pivot_lang, '英语')

    def test_direct_terms_override_combined_while_combined_fills_gaps(self) -> None:
        result = resolve_smart_glossary(
            [
                dataset(
                    'zh-es',
                    '中文',
                    '西班牙语',
                    {'你好': 'hola', '房子': 'casa'},
                ),
                dataset(
                    'zh-pt',
                    '中文',
                    '葡萄牙语',
                    {'你好': 'olá', '房子': 'casa portuguesa'},
                ),
                dataset(
                    'es-pt',
                    '西班牙语',
                    '葡萄牙语',
                    {'hola': 'cumprimento'},
                ),
            ],
            '西班牙语',
            '葡萄牙语',
        )

        self.assertEqual(result.entries['hola'], 'cumprimento')
        self.assertEqual(result.entry_origins['hola'], 'direct')
        self.assertEqual(result.entries['casa'], 'casa portuguesa')
        self.assertEqual(result.entry_origins['casa'], 'combined')

    def test_alias_entries_expand_before_combination(self) -> None:
        result = resolve_smart_glossary(
            [
                dataset(
                    'zh-es',
                    '中文',
                    '西班牙语',
                    {'你好/嗨': 'hola/qué tal'},
                ),
                dataset(
                    'zh-pt',
                    '中文',
                    '葡萄牙语',
                    {'你好': 'olá', '嗨': 'oi'},
                ),
            ],
            '西班牙语',
            '葡萄牙语',
        )

        self.assertEqual(result.entries, {'hola': 'olá', 'qué tal': 'oi'})

    def test_route_with_largest_common_coverage_wins(self) -> None:
        result = resolve_smart_glossary(
            [
                dataset('zh-es', '中文', '西班牙语', {'一': 'uno', '二': 'dos'}),
                dataset('zh-pt', '中文', '葡萄牙语', {'一': 'um', '二': 'dois'}),
                dataset('en-es', '英语', '西班牙语', {'one': 'uno'}),
                dataset('en-pt', '英语', '葡萄牙语', {'one': 'um'}),
            ],
            '西班牙语',
            '葡萄牙语',
        )

        self.assertEqual(result.entries, {'uno': 'um', 'dos': 'dois'})
        self.assertEqual(
            [route.pivot_lang for route in result.routes if route.kind == 'combined'],
            ['中文'],
        )


if __name__ == '__main__':
    unittest.main()
