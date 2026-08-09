"""Isolated acceptance evidence for T-020 EPUB parsing."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import types
import unittest
import warnings
import zipfile

BACKEND = Path(__file__).resolve().parents[1] / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Keep this parser acceptance test runnable with the host interpreter, which
# intentionally does not install the complete OpenWebUI application dependency
# set (for example typer).  The parser package itself has no app dependency.
if 'open_webui' not in sys.modules:
    package = types.ModuleType('open_webui')
    package.__path__ = [str(BACKEND / 'open_webui')]
    sys.modules['open_webui'] = package

from open_webui.retrieval.parsers.epub import EPUBParser
from open_webui.retrieval.parsers.epub.archive import EpubArchiveError
from open_webui.retrieval.parsers.epub.xhtml import extract_xhtml_text

FIXTURES = Path(__file__).parent / 'fixtures' / 'epub_parser'


def build_fixture_epub(target: Path, *, with_nav: bool = True) -> None:
    members = {
        'META-INF/container.xml': 'container.xml',
        'OPS/book.opf': 'book.opf',
        'OPS/nav.xhtml': 'nav.xhtml',
        'OPS/toc.ncx': 'toc.ncx',
        'OPS/text/chapter.xhtml': 'chapter.xhtml',
    }
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for destination, source in members.items():
            content = (FIXTURES / source).read_bytes()
            if destination == 'OPS/book.opf' and not with_nav:
                content = content.replace(
                    b'    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n', b''
                )
            archive.writestr(destination, content)


class TestEpubParserSDD(unittest.TestCase):
    def test_nav_precedence_fragment_breadcrumbs_and_faithful_units(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            epub = Path(directory) / 'fixture.epub'
            build_fixture_epub(epub)
            result = EPUBParser(epub).parse_book()

        self.assertEqual(result.book_title, '文本 EPUB')
        self.assertEqual(
            [passage.content_kind for passage in result.passages],
            ['paragraph', 'heading', 'paragraph', 'list_item', 'blockquote', 'pre'],
        )
        self.assertEqual(result.passages[0].content, '短。')  # short passages are source evidence too
        self.assertEqual(result.passages[2].content, 'Hello world & 你好，😀\nnext.')
        self.assertEqual(result.passages[3].content, '列表 项目')
        self.assertEqual(result.passages[5].content, '  first\n    second\n')
        self.assertEqual(result.passages[0].toc_path, ('第一章',))
        self.assertEqual(result.passages[2].toc_path, ('第一章', '开始'))
        self.assertEqual(result.passages[4].toc_path, ('第一章', '第二节'))
        warning_codes = {warning.code for warning in result.warnings}
        self.assertTrue({'nav_ncx_disagreement', 'table_content_ignored', 'image_content_ignored'} <= warning_codes)

    def test_ncx_fallback_keeps_fragment_breadcrumbs_when_nav_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            epub = Path(directory) / 'ncx.epub'
            build_fixture_epub(epub, with_nav=False)
            result = EPUBParser(epub).parse_book()

        self.assertEqual(result.passages[2].toc_path, ('错误的 NCX 标题', '错误的子节'))

    def test_nested_blocks_are_not_duplicated_and_fallback_is_conservative(self) -> None:
        nested = extract_xhtml_text('<body><blockquote><p>only once</p></blockquote></body>', 'nested.xhtml')
        self.assertEqual([(item.content_kind, item.content) for item in nested.units], [('paragraph', 'only once')])
        fallback = extract_xhtml_text('<body>  plain\n content &amp; punctuation。 </body>', 'fallback.xhtml')
        self.assertEqual(
            [(item.content_kind, item.content) for item in fallback.units],
            [('fallback', 'plain content & punctuation。')],
        )

        mixed = extract_xhtml_text('<body>before<p>paragraph</p>after</body>', 'mixed.xhtml')
        self.assertEqual(
            [(item.content_kind, item.content) for item in mixed.units],
            [('fallback', 'before'), ('paragraph', 'paragraph'), ('fallback', 'after')],
        )

    def test_archive_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            epub = Path(directory) / 'unsafe.epub'
            with zipfile.ZipFile(epub, 'w') as archive:
                archive.writestr('../outside', 'bad')
            with self.assertRaises(EpubArchiveError):
                EPUBParser(epub).parse_book()

    def test_archive_duplicate_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            epub = Path(directory) / 'duplicate.epub'
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                with zipfile.ZipFile(epub, 'w') as archive:
                    archive.writestr('META-INF/container.xml', 'first')
                    archive.writestr('META-INF/container.xml', 'second')
            with self.assertRaises(EpubArchiveError):
                EPUBParser(epub).parse_book()


if __name__ == '__main__':
    unittest.main()
