import re
import gc
import uuid

import numpy as np
import asyncio
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from langchain_core.documents import Document
from open_webui.config import RAG_EMBEDDING_CONTENT_PREFIX
from open_webui.utils.bilingual.dpalign_fixed import AlignedPair, DPAlignMixin, SentBlock, AlignContext


import time
import logging

logger = logging.getLogger(__name__)


class StageTimer:
    """简单的阶段计时器，用于快速定位耗时占比"""

    def __init__(self, name: str):
        self.name = name
        self.start = None

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *args):
        elapsed = time.perf_counter() - self.start
        logger.info(f'[STAGE] {self.name}: {elapsed:.3f}s')


import cProfile
import pstats
import io
import functools
import time


def profile_async(top_n: int = 30, sort_by: str = 'cumulative'):
    """
    异步函数专用 profiling 装饰器。
    打印耗时占比最高的 top_n 个函数调用。
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            profiler = cProfile.Profile()
            profiler.enable()
            start = time.time()
            try:
                result = await func(*args, **kwargs)
                return result
            finally:
                elapsed = time.time() - start
                profiler.disable()

                stream = io.StringIO()
                stats = pstats.Stats(profiler, stream=stream).sort_stats(sort_by)
                stats.print_stats(top_n)

                print(f'\n{"=" * 80}')
                print(f'[PROFILE] {func.__name__} 总耗时: {elapsed:.2f}s')
                print(f'{"=" * 80}')
                print(stream.getvalue())
                print(f'{"=" * 80}\n')

        return wrapper

    return decorator


class BilingualSplitter:
    _OPEN_TO_CLOSE = {
        '(': ')',
        '[': ']',
        '{': '}',
        '\u201c': '\u201d',  # " → "
        '\u300c': '\u300d',  # 「→ 」
        '\u300e': '\u300f',  # 『→ 』
        '\u3010': '\u3011',  # 【→ 】
        '\uff08': '\uff09',  # （→ ）
    }
    _CLOSE_CHARS = set(_OPEN_TO_CLOSE.values())

    def __init__(self, model_name: str = 'sat-3l-sm'):
        self._model_name = model_name
        self._sat = None
        self._sat_load_attempted = False
        self._sat_lock = threading.Lock()

    def _ensure_sat(self):
        if self._sat is not None or self._sat_load_attempted:
            return self._sat

        with self._sat_lock:
            if self._sat is not None or self._sat_load_attempted:
                return self._sat
            self._sat_load_attempted = True
            try:
                from wtpsplit_lite import SaT

                started_at = time.perf_counter()
                logger.info('Loading bilingual sentence splitter on first use')
                self._sat = SaT(
                    self._model_name,
                    ort_providers=['CUDAExecutionProvider', 'CPUExecutionProvider'],
                )
                logger.info(
                    'Loaded bilingual sentence splitter in %.2fs',
                    time.perf_counter() - started_at,
                )
            except Exception as e:
                logger.warning('Failed to load bilingual sentence splitter: %s', e)
            return self._sat

    def release(self):
        """释放底层 SaT 模型占用的内存/显存"""
        if self._sat is not None:
            del self._sat
            self._sat = None
        self._sat_load_attempted = True
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def reload(self, model_name: str = 'sat-3l-sm'):
        """重新加载模型（release 之后如果还要用）"""
        self._model_name = model_name
        self._sat = None
        self._sat_load_attempted = False
        self._ensure_sat()

    @staticmethod
    def _split_paragraphs(text: str) -> list[str]:
        paras = re.split(r'\n{1,}', text)
        return [p.strip() for p in paras if p.strip()]

    @classmethod
    def _is_balanced(cls, text: str) -> bool:
        stack = []
        for ch in text:
            if ch in cls._OPEN_TO_CLOSE:
                stack.append(cls._OPEN_TO_CLOSE[ch])
            elif ch in cls._CLOSE_CHARS:
                if not stack or stack[-1] != ch:
                    return False
                stack.pop()
        return len(stack) == 0

    @classmethod
    def _merge_unbalanced(cls, sentences: list[str]) -> list[str]:
        merged, buffer = [], ''
        for sent in sentences:
            buffer = (buffer + ' ' + sent).strip() if buffer else sent
            if cls._is_balanced(buffer):
                merged.append(buffer)
                buffer = ''
        if buffer:
            merged.append(buffer)
        return merged

    @classmethod
    def _merge_ellipsis(cls, sentences: list[str]) -> list[str]:
        """
        把以省略号结尾的句子和下一句合并。
        处理以下几种省略号形式：
        …  （单字符省略号 U+2026）
        ..  （两个点，不常见但存在）
        ... （三个点）
        ......（六个点，中文常见）
        """
        if not sentences:
            return []

        _ELLIPSIS_END = re.compile(r'(…+|\.{2,})\s*$')

        result = []
        buffer = ''
        for sent in sentences:
            if buffer:
                buffer = buffer + ' ' + sent
            else:
                buffer = sent

            # 如果不是省略号结尾，就可以输出了
            if not _ELLIPSIS_END.search(buffer):
                result.append(buffer.strip())
                buffer = ''

        if buffer:
            result.append(buffer.strip())
        return result

    @classmethod
    def _merge_short(cls, sentences: list[str], min_chars: int = 30) -> list[str]:
        """把过短的句子合并到前一句，若是第一句则合并到后一句。"""
        if not sentences:
            return []
        result = []
        i = 0
        while i < len(sentences):
            sent = sentences[i]
            if len(sent) < min_chars:
                if result:
                    result[-1] = result[-1] + ' ' + sent
                elif i + 1 < len(sentences):
                    sentences[i + 1] = sent + ' ' + sentences[i + 1]
                else:
                    result.append(sent)
            else:
                result.append(sent)
            i += 1
        return result

    @classmethod
    def merge_sentences(cls, sentences: list[str], max_lengths: int) -> list[str]:
        balanced = cls._merge_unbalanced(sentences)
        merged = cls._merge_ellipsis(balanced)
        deduped = cls._merge_short(merged, max_lengths)
        return deduped

    @classmethod
    def _calc_offsets(cls, para_sents: list[list[str]]) -> list[tuple[int, int]]:
        """计算每段句子在展平列表中的 [start, end) 位置"""
        offsets = []
        cursor = 0
        for sents in para_sents:
            offsets.append((cursor, cursor + len(sents)))
            cursor += len(sents)
        return offsets

    def split(self, text: str, max_length=30, lang: str = 'zh') -> list[str]:
        text = text.strip()
        if not text:
            return []

        sat = self._ensure_sat()
        if sat is not None:
            try:
                parts = sat.split(text, stride=128, block_size=256)
                parts = [s.strip() for s in parts if s.strip()]
                return self.merge_sentences(parts, max_length)
            except Exception as e:
                print(e)
                pass

        return [text]

    def split_batch(self, texts: list[str], max_length: int = 40) -> list[list[str]]:
        texts = [t.strip() for t in texts]
        non_empty_idx = [i for i, t in enumerate(texts) if t]
        if not non_empty_idx:
            return [[] for _ in texts]

        non_empty_texts = [texts[i] for i in non_empty_idx]
        sat = self._ensure_sat()
        if sat is not None:
            try:
                batch_parts = sat.split(non_empty_texts, stride=128, block_size=256, threshold=0.7)
                results = [[] for _ in texts]
                for idx, parts in zip(non_empty_idx, batch_parts):
                    parts = [s.strip() for s in parts if s.strip()]
                    results[idx] = self.merge_sentences(parts, max_length)
                return results
            except Exception as e:
                print(e)
                # 失败则降级为整段返回原文本
                pass

        results = [[] for _ in texts]
        for idx in non_empty_idx:
            results[idx] = [texts[idx]]
        return results


class BilingualAligner:
    """
    Vecalign 双语句子对齐器。

    参数说明：
        overlap_n:  重叠窗口大小，默认 4]
                    文档较短可用 2，节省 embedding 调用
        max_align:  最大对齐句数组合，默认 4（即最多 4:4）
        min_score:  低于此余弦相似度时译文置为空
    """

    def __init__(self, splitter):
        self.splitter = splitter
        self.align_mix = DPAlignMixin()
        self._split_sem = asyncio.Semaphore(2)
        self._dp_sem = asyncio.Semaphore(4)
        self._split_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='sat-split')
        self._dp_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='dp-align')

    def set_embed_fn(self, embed_fn, user, request):
        self._embed_fn = embed_fn
        self._user = user
        self._request = request

    @staticmethod
    async def _report_progress(
        progress_callback: Callable[[dict], Awaitable[None]] | None,
        stage: str,
        progress: int,
        **details,
    ) -> None:
        if progress_callback is not None:
            await progress_callback({'stage': stage, 'progress': progress, **details})

    def split(self, text: str, lang: str) -> list[str]:
        return self.splitter.split(text, lang=lang)

    async def _split_paras_concurrently(self, paras: list[str], lang: str) -> list[list[str]]:
        if not paras:
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._split_executor, self.splitter.split_batch, paras)

    async def _split_one_text(self, text: str, lang: str) -> tuple[list[str], list[list[str]]]:
        async with self._split_sem:
            logger.info(f'[切割] 开始切割文本，长度 {len(text)}，语言 {lang}')
            paras = BilingualSplitter._split_paragraphs(text.strip())
            para_sents = await self._split_paras_concurrently(paras, lang)
            return paras, para_sents

    async def _align_blocks(
        self,
        src_blocks: list[SentBlock],
        tgt_blocks_map: dict[str, list[SentBlock]],
        src_paras: list[str],
        tgt_paras_map: dict[str, list[str]],
    ) -> list[AlignedPair]:

        all_pairs: list[AlignedPair] = []

        async def _align_one(para_index: int, lang: str, src_block, tgt_block, src_para, tgt_para):
            async with self._dp_sem:
                if len(src_block.sents) == 1 and len(tgt_block.sents) == 1:
                    pairs = [
                        AlignedPair(
                            source=src_block.sents[0],
                            target=tgt_block.sents[0],
                            src_indices=[0],
                            tgt_indices=[0],
                            score=1.0,
                            para_index=para_index,
                            para_source=src_para,
                            para_target=tgt_para,
                        )
                    ]
                else:
                    pairs = await asyncio.to_thread(
                        self._dp_align, src_block, tgt_block, para_index, src_para, tgt_para
                    )
                return para_index, lang, pairs

        tasks = []
        for para_index, src_block in enumerate(src_blocks):
            src_para = src_paras[para_index]

            lang_results: dict[str, list[AlignedPair]] = {}
            for lang, tgt_blocks in tgt_blocks_map.items():
                if para_index >= len(tgt_blocks):
                    continue
                tgt_block = tgt_blocks[para_index]
                tgt_para = tgt_paras_map[lang][para_index]
                tasks.append(_align_one(para_index, lang, src_block, tgt_block, src_para, tgt_para))

        results = await asyncio.gather(*tasks)
        grouped: dict[int, dict[str, list[AlignedPair]]] = {}
        for para_index, lang, pairs in results:
            grouped.setdefault(para_index, {})[lang] = pairs

        for para_index in sorted(grouped.keys()):
            lang_results = grouped[para_index]
            if not lang_results:
                continue

            first_lang = next(iter(lang_results))
            base_pairs = lang_results[first_lang]

            # 其余语言建立 src_indices_key → pair 的索引
            other_index: dict[str, dict[tuple, AlignedPair]] = {}
            other_para_index: dict[str, dict[tuple, AlignedPair]] = {}
            for lang, pairs in lang_results.items():
                other_index[lang] = {tuple(p.src_indices): p for p in pairs}
                other_para_index[lang] = {p.para_index: p for p in pairs}

            for base_pair in base_pairs:
                extra: dict[str, str] = {}
                for lang, idx in other_index.items():
                    matched = idx.get(tuple(base_pair.src_indices))
                    extra[lang] = matched.target if matched else ''

                extra_para: dict[str, str] = {}
                for lang, idx in other_para_index.items():
                    matched = idx.get(base_pair.para_index)
                    extra_para[lang] = matched.para_target if matched else ''

                base_pair.extra_targets = extra
                base_pair.para_target = extra_para
                all_pairs.append(base_pair)

        return all_pairs

    def _normalize(self, embs: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(embs, axis=1, keepdims=True)
        return embs / np.where(norms == 0, 1, norms)

    async def _embed_both(
        self,
        src_para_sents: list[list[str]] | None,
        tgt_para_sents_map: dict[str, list[list[str]]],
    ) -> tuple[list[SentBlock], list[SentBlock]]:
        if self._embed_fn is None:
            raise RuntimeError('embed_fn 未设置，请通过构造函数或 set_embed_fn() 传入。')

        src_all = [s for sents in src_para_sents for s in sents] if src_para_sents else []
        tgt_all_map: dict[str, list[str]] = {}
        for lang, para_sents in tgt_para_sents_map.items():
            tgt_all_map[lang] = [s for sents in para_sents for s in sents]

        all_texts = [t.replace('\n', ' ') for t in src_all]
        for sents in tgt_all_map.values():
            all_texts += [t.replace('\n', ' ') for t in sents]

        embeddings = await self._embed_fn(
            all_texts,
            prefix=RAG_EMBEDDING_CONTENT_PREFIX,
            user=self._user,
        )

        all_embs = self._normalize(np.array(embeddings))
        cursor = 0

        # src blocks
        src_blocks, cursor = [], 0
        for sents in src_para_sents:
            n = len(sents)
            src_blocks.append(SentBlock(sents=sents, embs=all_embs[cursor : cursor + n]))
            cursor += n

        # tgt blocks
        tgt_blocks_map: dict[str, list[SentBlock]] = {}
        for lang in tgt_all_map.keys():
            blocks = []
            for sents in tgt_para_sents_map[lang]:
                n = len(sents)
                blocks.append(SentBlock(sents=sents, embs=all_embs[cursor : cursor + n]))
                cursor += n
            tgt_blocks_map[lang] = blocks
        return src_blocks, tgt_blocks_map

    def _dp_align(
        self, src: SentBlock, tgt: SentBlock, para_index: int = 0, para_source: str = '', para_target: str = ''
    ):
        ctx = AlignContext(para_index=para_index, para_source=para_source, para_target=para_target)

        return self.align_mix._dp_align(src=src, tgt=tgt, ctx=ctx)

    async def align_to_documents(self, src_text, tgt_texts, src_lang, metadata=None):
        docs_list = await self.align_batch_to_documents(
            [{'src_text': src_text, 'tgt_texts': tgt_texts, 'src_lang': src_lang, 'metadata': metadata or {}}]
        )
        return docs_list[0]

    async def align_batch_to_documents(
        self,
        items: list[dict],
        progress_callback: Callable[[dict], Awaitable[None]] | None = None,
    ) -> list[list[Document]]:
        n = len(items)
        if n == 0:
            return []

        await self._report_progress(
            progress_callback,
            'splitting',
            12,
            current=0,
            total=n,
        )
        self.splitter.reload()

        with StageTimer(f'分段+切句(批量,{n}个文件)'):
            split_tasks = []
            for i, item in enumerate(items):
                split_tasks.append(self._split_one_text(item['src_text'].strip(), item['src_lang']))
            src_split_results = await asyncio.gather(*split_tasks)  # [(paras, para_sents), ...]
            logger.info(f'[切割] 完成 {n} 个文件的源语言切割')

            tgt_split_tasks = []
            tgt_task_index = []
            for i, item in enumerate(items):
                for lang, text in item['tgt_texts'].items():
                    tgt_split_tasks.append(self._split_one_text(text.strip(), lang))
                    tgt_task_index.append((i, lang))
            tgt_split_results = await asyncio.gather(*tgt_split_tasks) if tgt_split_tasks else []
            logger.info(f'[切割] 完成 {len(tgt_split_results)} 个目标语言切割')

            # 整理回每个 item 的结构
            src_paras_list = [r[0] for r in src_split_results]
            src_para_sents_list = [r[1] for r in src_split_results]

            tgt_paras_map_list: list[dict[str, list[str]]] = [dict() for _ in range(n)]
            tgt_para_sents_map_list: list[dict[str, list[list[str]]]] = [dict() for _ in range(n)]
            for (item_idx, lang), (paras, para_sents) in zip(tgt_task_index, tgt_split_results):
                tgt_paras_map_list[item_idx][lang] = paras
                tgt_para_sents_map_list[item_idx][lang] = para_sents
        self.splitter.release()

        with StageTimer(f'Embedding调用(批量,{n}个文件)'):
            logger.info(f'[Embedding] 开始 {n} 个文件的 embedding 调用')
            all_texts: list[str] = []
            src_slices: list[list[tuple[int, int]]] = []  # 每个item: 每段的(start,end)
            tgt_slices: list[dict[str, list[tuple[int, int]]]] = [dict() for _ in range(n)]

            cursor = 0
            for i in range(n):
                slices = []
                for sents in src_para_sents_list[i]:
                    s, e = cursor, cursor + len(sents)
                    slices.append((s, e))
                    all_texts.extend(t.replace('\n', ' ') for t in sents)
                    cursor = e
                src_slices.append(slices)

            for i in range(n):
                for lang, para_sents in tgt_para_sents_map_list[i].items():
                    slices = []
                    for sents in para_sents:
                        s, e = cursor, cursor + len(sents)
                        slices.append((s, e))
                        all_texts.extend(t.replace('\n', ' ') for t in sents)
                        cursor = e
                    tgt_slices[i][lang] = slices

            if not all_texts:
                return [[] for _ in range(n)]

            await self._report_progress(
                progress_callback,
                'alignment_embeddings',
                24,
                current=0,
                total=len(all_texts),
            )
            logger.info(f'[Embedding] 总共 {len(all_texts)} 个句子，开始调用 embedding 函数')
            embeddings = await self._embed_fn(
                all_texts,
                prefix=RAG_EMBEDDING_CONTENT_PREFIX,
                user=self._user,
            )
            all_embs = self._normalize(np.array(embeddings))

            # 3. 按切片切回每个 item 的 SentBlock
            src_blocks_list: list[list[SentBlock]] = []
            for i in range(n):
                blocks = []
                for (s, e), sents in zip(src_slices[i], src_para_sents_list[i]):
                    blocks.append(SentBlock(sents=sents, embs=all_embs[s:e]))
                src_blocks_list.append(blocks)

            tgt_blocks_map_list: list[dict[str, list[SentBlock]]] = [dict() for _ in range(n)]
            for i in range(n):
                for lang, para_sents in tgt_para_sents_map_list[i].items():
                    blocks = []
                    for (s, e), sents in zip(tgt_slices[i][lang], para_sents):
                        blocks.append(SentBlock(sents=sents, embs=all_embs[s:e]))
                    tgt_blocks_map_list[i][lang] = blocks

        with StageTimer(f'DP对齐(批量,{n}个文件)'):
            # 4. 每个 item 独立做 DP 对齐（仍然并发，但不再重复切句/embedding）
            logger.info(f'[对齐] 开始 {n} 个文件的 DP 对齐')
            all_pairs_list = []
            for i in range(n):
                await self._report_progress(
                    progress_callback,
                    'aligning_paragraphs',
                    50 + round((i / max(n, 1)) * 12),
                    current=i,
                    total=n,
                )
                all_pairs_list.append(
                    await self._align_blocks(
                        src_blocks_list[i],
                        tgt_blocks_map_list[i],
                        src_paras_list[i],
                        tgt_paras_map_list[i],
                    )
                )

        await self._report_progress(
            progress_callback,
            'assembling_chunks',
            62,
            current=n,
            total=n,
        )

        # 5. 组装成 Document
        result: list[list[Document]] = []
        for i, item in enumerate(items):
            src_lang = item['src_lang']
            base_meta = item.get('metadata') or {}
            docs = []
            for idx, pair in enumerate(all_pairs_list[i]):
                if not pair.source.strip():
                    continue

                all_lang_texts = {src_lang: pair.source, **pair.extra_targets}
                align_group_id = str(uuid.uuid4())
                shared_metadata = {
                    **base_meta,
                    'type': 'sentence',
                    'align_group_id': align_group_id,
                    'para_index': pair.para_index,
                    'sentence_index': idx,
                    'align_score': pair.score,
                    'parent_content': pair.para_source,
                    'parent_langs': {src_lang: pair.para_source, **pair.para_target},
                    'primary_lang': src_lang,
                }

                for lang_code, text in all_lang_texts.items():
                    if not text.strip():
                        continue
                    docs.append(
                        Document(
                            page_content=text,
                            metadata={
                                **shared_metadata,
                                'lang': lang_code,
                            },
                        )
                    )
            result.append(docs)

        return result


bilingual_spliter = BilingualSplitter()
bilingual_aligner = BilingualAligner(bilingual_spliter)
