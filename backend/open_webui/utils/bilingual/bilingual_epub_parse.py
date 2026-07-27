import os
import re
from collections import deque
from pathlib import Path
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

toc_dict = {}


def sanitize_filename(name: str) -> str:
    """清理文件名，移除非法字符"""
    invalid = '<>:"/\\|?*'
    for char in invalid:
        name = name.replace(char, '_')
    return name.strip()[:100]  # 限制长度


def extract_chapter_content(item) -> str:
    if not item:
        return ''

    content = item.get_content().decode('utf-8')
    soup = BeautifulSoup(content, 'html.parser')

    for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
        tag.decompose()

    # 尝试获取标题
    text_parts = []
    for h in soup.find_all(['h1', 'h2', 'h3']):
        title = h.get_text().strip()
        if title and len(title) > 3:
            text_parts.append(title)
            break

    # 提取正文段落
    paragraphs = soup.find_all(['p', 'h2', 'h3', 'div', 'li'])
    for p in paragraphs:
        para_text = p.get_text().strip()
        if para_text and len(para_text) > 10:
            if not text_parts or para_text != text_parts[0]:
                text_parts.append(para_text)

    return '\n\n'.join(text_parts)


def process_toc_with_queue(book, output_dir: Path, lang: str):
    if not book.toc:
        print('❌ 未找到目录结构')
        return

    queue = deque()
    for item in book.toc:
        queue.append((item, ''))

    seen = set()
    chapter_counter = 1
    while queue:
        current, parent_prefix = queue.popleft()
        if isinstance(current, tuple) and len(current) == 2:
            section, children = current
            section_name = section.title if hasattr(section, 'title') else str(section)
            section_name = sanitize_filename(section_name)
            new_prefix = f'{parent_prefix}{section_name}_' if parent_prefix else f'{section_name}_'
            for child in children:
                queue.append((child, new_prefix))

        elif isinstance(current, ebooklib.epub.Link):
            title = current.title or f'章节_{chapter_counter:03d}'
            href = current.href.split('#')[0]
            if title in seen:
                print(f'  [{lang}] 跳过重复章节: {title} ({href})')
                chapter_counter += 1
                continue
            seen.add(title)

            chapter_item = book.get_item_with_href(href)
            content = extract_chapter_content(chapter_item) if chapter_item else ''
            if content:
                clean_title = sanitize_filename(title)
                filename = f'{clean_title}.txt'
                if lang not in toc_dict:
                    toc_dict[lang] = [filename]
                else:
                    toc_dict[lang].append(filename)

                output_path = output_dir / filename
                if output_path.exists():
                    print(f'已经存在了{output_path}')
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(content)
            chapter_counter += 1

    print('-' * 50)
    print(f'保存了{len(toc_dict[lang])}条文本')
    print('-' * 50)


def epub_to_chapters(epub_path: str, output_dir: str = './txt', lang='zh'):
    book = epub.read_epub(epub_path)

    output_dir_path = Path(output_dir)
    output_dir_path.mkdir(parents=True, exist_ok=True)

    print('📖 EPUB 标题:', book.get_metadata('DC', 'title'))
    print('📋 开始按目录提取章节...\n')

    if not book.toc:
        print('❌ 未找到目录结构，使用旧方法处理所有文档...')
        return

    process_toc_with_queue(book, output_dir_path, lang)
    print('\n🎉 全部章节提取完成！')


def extract_chapters_from_epub(epub_path: str, lang: str = 'zh'):
    """
    从EPUB中提取章节

    返回格式:
    {
        'title': str,
        'chapters': [
            {
                'title': str,
                'content': str,
                'index': int
            },
            ...
        ],
        'language': str
    }
    """
    try:
        book = epub.read_epub(epub_path)
    except Exception as e:
        print(f'Failed to read EPUB: {e}')
        raise ValueError(f'Failed to read EPUB file: {str(e)}')

    if not book.toc:
        print('No TOC found in EPUB, using fallback method')
        raise ValueError('EPUB must have a valid table of contents')

    chapters = []
    seen = set()
    chapter_counter = 1
    queue = deque()

    # 初始化队列
    for item in book.toc:
        queue.append((item, ''))

    # BFS 处理目录
    while queue:
        current, parent_prefix = queue.popleft()

        if isinstance(current, tuple) and len(current) == 2:
            section, children = current
            section_name = section.title if hasattr(section, 'title') else str(section)
            section_name = sanitize_filename(section_name)
            new_prefix = f'{parent_prefix}{section_name}_' if parent_prefix else f'{section_name}_'
            for child in children:
                queue.append((child, new_prefix))

        elif isinstance(current, epub.Link):
            title = current.title
            href = current.href.split('#')[0]

            if title in seen:
                print(f'Skipping duplicate chapter: {title}')
                chapter_counter += 1
                continue

            seen.add(title)

            try:
                chapter_item = book.get_item_with_href(href)
                content = extract_chapter_content(chapter_item) if chapter_item else ''

                if content.strip():
                    chapters.append({'title': title, 'content': content, 'index': chapter_counter - 1})
                    chapter_counter += 1
            except Exception as e:
                print(f'Failed to extract chapter {title}: {e}')
                continue

    if not chapters:
        raise ValueError('No chapters extracted from EPUB')

    try:
        book_title = book.get_metadata('DC', 'title')[0][0] if book.get_metadata('DC', 'title') else 'Unknown'
    except:
        book_title = 'Unknown'

    return {'title': book_title, 'chapters': chapters, 'language': lang, 'chapter_count': len(chapters)}


def get_epub_list():
    epub_list = []
    for epub_name in os.listdir('./epub'):
        epub_path = os.path.join('./epub', epub_name)

        lang, _ = parse_file_name(epub_name)
        epub_list.append((epub_path, lang))
    return epub_list


def parse_file_name(file_name: str):
    lang_pattern = r'[a-zA-Z]{2}(?:[-_][a-zA-Z]{2})?'
    pattern = re.compile(
        rf'^(?P<lang1>{lang_pattern})[-_.](?P<name1>.+)$|'
        rf'^(?P<name2>.+?)[-_.](?P<lang2>{lang_pattern})$',
        re.IGNORECASE,
    )

    match = pattern.match(os.path.splitext(file_name.strip())[0])
    if not match:
        return None, None

    gd = match.groupdict()
    if gd['lang1'] and gd['name1']:
        lang = gd['lang1'].lower()
        name = gd['name1']
    elif gd['lang2'] and gd['name2']:
        lang = gd['lang2'].lower()
        name = gd['name2']
    else:
        return None, None

    if not (2 <= len(lang) <= 5):
        return None, None
    return lang, name


def rename_file():
    primary_lang = 'zh'
    primary_list = toc_dict.pop(primary_lang, [])

    def __rename_file(path: str, title: str, lang: str):
        file_name = os.path.basename(title)
        dir_name = os.path.dirname(path)
        name, subfix = os.path.splitext(str(file_name))
        new_name = f'{name}_{lang}{subfix}'
        new_path = os.path.join(str(dir_name), new_name)
        if not os.path.exists(path):
            print(f'没有找到{path}')
            return
        os.rename(path, new_path)

    epub_dir_path = './txt'
    for index, title in enumerate(primary_list):
        primary_txt_path = os.path.join(epub_dir_path, primary_lang, title)
        __rename_file(primary_txt_path, title, primary_lang)
        for lang, toc_list in toc_dict.items():
            txt_path = os.path.join(epub_dir_path, lang, toc_list[index])
            __rename_file(txt_path, title, lang)


def run():
    epub_list = get_epub_list()
    for epub_path, lang in epub_list:
        epub_to_chapters(epub_path, f'./txt/{lang}', lang)
    rename_file()
