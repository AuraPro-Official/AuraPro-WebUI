import re
import langcodes
import threading
import logging
import unicodedata
import jieba
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import stanza

logger = logging.getLogger('bilingual_align')

try:
    # https://github.com/cgohlke/pyicu-build/releases/latest
    from icu import BreakIterator, Locale
    _ICU_AVAILABLE = True
except ImportError:
    _ICU_AVAILABLE = False

try:
    from wordfreq import zipf_frequency
    _WORDFREQ_AVAILABLE = True
except ImportError:
    _WORDFREQ_AVAILABLE = False


class PhraseNormalizer:
    @classmethod
    def normalize(cls, phrase: str, lang: str, tokens: list[str] | None = None, lemmas: list[str] | None = None) -> str:
        if lang == 'zh':
            return cls._normalize_zh(phrase)
        return cls._normalize_en(phrase, tokens, lemmas)

    @classmethod
    def _normalize_en(cls, phrase: str, tokens: list[str] | None, lemmas: list[str] | None) -> str:
        if lemmas and tokens and len(lemmas) == len(tokens):
            words = lemmas
        else:
            words = phrase.split()
        norm = ' '.join(w.lower() for w in words)
        norm = norm.replace('-', ' ')
        norm = re.sub(r'[^\w\s]', '', norm)
        norm = re.sub(r'\s+', ' ', norm).strip()
        return norm

    @classmethod
    def _normalize_zh(cls, phrase: str) -> str:
        # 全角 -> 半角
        norm = ''.join(
            chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch
            for ch in phrase
        )
        norm = norm.replace('\u3000', ' ')
        norm = unicodedata.normalize('NFKC', norm)
        norm = re.sub(r'\s+', '', norm) 
        return norm.strip()



class POSFilter:
    """ 基于 Stanza 的词性标注器 """
    NOUN_TAGS = {'NOUN', 'PROPN'}
    CONTENT_TAGS = {'NOUN', 'PROPN', 'VERB', 'ADJ', 'ADV'}
    FUNCTION_TAGS = {'ADP', 'DET', 'PRON', 'CCONJ', 'SCONJ', 'AUX', 'PART', 'NUM', 'PUNCT', 'SYM', 'INTJ'}

    _pipelines: dict[str, 'stanza.Pipeline'] = {}
    _lock = threading.Lock()
    _unsupported_langs: set[str] = set()

    @classmethod
    def _get_pipeline(cls, lang: str):
        import stanza
        if lang in cls._unsupported_langs:
            return None
        if lang not in cls._pipelines:
            with cls._lock:
                if lang not in cls._pipelines and lang not in cls._unsupported_langs:
                    try:
                        stanza.download(lang, processors='tokenize,pos', verbose=False)
                        cls._pipelines[lang] = stanza.Pipeline(
                            lang=lang,
                            processors='tokenize,pos',
                            tokenize_pretokenized=True,  
                            verbose=False,
                        )
                    except Exception as e:
                        logger.warning('语言 %s 没有可用的 Stanza POS 模型，已跳过词性过滤: %s', lang, e)
                        cls._unsupported_langs.add(lang)
                        return None
        return cls._pipelines.get(lang)
    
    @classmethod
    def tag_tokens(cls, tokens: list[str], lang: str) -> list[str] | None:
        if not tokens:
            return []
        pipeline = cls._get_pipeline(lang)
        if pipeline is None:
            return None
        text = ' '.join(tokens)  
        doc = pipeline(text)
        tags = [word.upos for sent in doc.sentences for word in sent.words]
        if len(tags) != len(tokens):
            return None
        return tags
    
    @classmethod
    def has_noun(cls, tokens: list[str], lang: str) -> bool:
        tags = cls.tag_tokens(tokens, lang)
        if tags is None:
            return True
        return any(t in cls.NOUN_TAGS for t in tags)
    
    @classmethod
    def has_content_word(cls, tokens: list[str], lang: str) -> bool:
        tags = cls.tag_tokens(tokens, lang)
        if tags is None:
            return True
        return any(t in cls.CONTENT_TAGS for t in tags)

    @classmethod
    def is_function_word_only(cls, pos_tags: list[str], start: int, end: int) -> bool:
        window_tags = [pos_tags[i] for i in range(start, end) if i < len(pos_tags)]
        if not window_tags:
            return True
        return all(t in cls.FUNCTION_TAGS for t in window_tags)
    
    @classmethod
    def tag_and_lemmatize(cls, tokens: list[str], lang: str) -> tuple[list[str] | None, list[str] | None]:
        """同时返回 (pos_tags, lemmas)，失败时各自为 None。"""
        if not tokens:
            return [], []
        pipeline = cls._get_pipeline(lang)
        if pipeline is None:
            return None, None
        text = ' '.join(tokens)
        doc = pipeline(text)
        words = [word for sent in doc.sentences for word in sent.words]
        if len(words) != len(tokens):
            return None, None
        tags = [w.upos for w in words]
        lemmas = [w.lemma or tok for w, tok in zip(words, tokens)]
        return tags, lemmas


    
class CandidateGenerator:
    ALLOW = {'NOUN', 'PROPN', 'ADJ', 'VERB'}
    END_TAGS = {'NOUN', 'PROPN'}
    STRICT_ALLOW = {'NOUN', 'PROPN', 'ADJ', 'VERB', 'ADV'}

    def generate(self, tokens: list[str], pos_tags: list[str], max_len: int = 5) -> list[tuple[int, int]]:
        if pos_tags is None:
            n = len(tokens)
            return []

        candidates = []
        n = len(tokens)

        for i in range(n):
            if pos_tags[i] not in self.ALLOW:
                continue

            for j in range(i + 1, min(i + max_len + 1, n + 1)):
                window_tags = pos_tags[i:j]
                if any(tag in POSFilter.FUNCTION_TAGS for tag in window_tags):
                    break

                if all(tag in self.STRICT_ALLOW for tag in window_tags):
                    if pos_tags[j-1] in self.END_TAGS:
                        candidates.append((i, j))
                else:
                    break
        return candidates


class TermhoodScorer:
    COMMON_THRESHOLD = 4.0
    _warned_langs: set[str] = set()

    @classmethod
    def _warn_once(cls, lang: str):
        if lang not in cls._warned_langs:
            cls._warned_langs.add(lang)
            logger.warning('语言 %s 不在 wordfreq 支持范围内，已跳过术语性词频过滤', lang)

    @classmethod
    def phrase_zipf(cls, phrase: str, lang: str) -> float | None:
        if not _WORDFREQ_AVAILABLE:
            return None
        tokens = phrase.split()
        if not tokens:
            return None
        try:
            scores = [zipf_frequency(t, lang) for t in tokens]
        except Exception:
            cls._warn_once(lang)
            return None
        return sum(scores) / len(scores)

    @classmethod
    def is_too_common(cls, phrase: str, lang: str, threshold: float | None = None) -> bool:
        avg_zipf = cls.phrase_zipf(phrase, lang)
        if avg_zipf is None:
            return False
        return avg_zipf >= (threshold or cls.COMMON_THRESHOLD)


class Tokenizer:
    _warned_no_icu = False
    _warned_langs: set[str] = set()
    _VI_NOTE = True

    @classmethod
    def _normalize_lang_key(cls, lang_name: str) -> str:
        lang_name = lang_name.strip()
        if not lang_name:
            return 'en'
        try:
            lang = langcodes.find(lang_name)
            if isinstance(lang.language, str) and lang.language.startswith('zh'):
                return 'zh'
            if isinstance(lang.language, str) and lang.language:
                return lang.language
            else:
                return lang_name.lower().split('-')[0].split('_')[0]
        except LookupError:
            return lang_name.casefold()

    @classmethod
    def _tokenize_icu(cls, text: str, lang: str) -> list[str]:
        lang_key = cls._normalize_lang_key(lang)
        try:
            locale = Locale(lang_key)
        except Exception:
            locale = Locale('en')

        boundary = BreakIterator.createWordInstance(locale)
        boundary.setText(text)

        words = []
        start = boundary.first()
        for end in boundary:
            piece = text[start:end].strip()
            if piece and not re.fullmatch(r'[\s]+', piece):
                words.append(piece)
            start = end
        return words

    @classmethod
    def _tokenize_fallback(cls, text: str) -> list[str]:
        return [t for t in re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE) if t.strip()]

    @classmethod
    def _warn_once(cls, key: str, msg: str):
        if key not in cls._warned_langs:
            cls._warned_langs.add(key)
            logger.warning(msg)

    @classmethod
    def tokenize(cls, text: str, lang: str) -> str:
        text = (text or '').strip()
        if not text:
            return ''
        
        if lang == "zh":
            words = list(jieba.cut(text))
            return words

        if not _ICU_AVAILABLE:
            if not cls._warned_no_icu:
                cls._warned_no_icu = True
                logger.warning('PyICU 未安装，已降级为正则分词，无空格语言(zh/th/lo/km/my等)分词粒度会变粗，建议 pip install PyICU')
            words = cls._tokenize_fallback(text)
        else:
            words = cls._tokenize_icu(text, lang)
        return words
    

class WordAligner:
    _instance: 'WordAligner | None' = None

    def __init__(
        self,
        model: str = 'bert',
        matching_method: str = 'a', 
        layer: int = 8,
    ):

        from simalign import SentenceAligner
        self._aligner = SentenceAligner(
            model=model,
            token_type='bpe',
            matching_methods=matching_method,
            layer=layer,
        )
        self._matching_key = {'a': 'inter', 'i': 'itermax', 'm': 'mwmf'}.get(
            matching_method, 'itermax'
        )

    @classmethod
    def get_instance(cls) -> 'WordAligner':
        if cls._instance is None:
            cls._instance = WordAligner()
        return cls._instance

    def align(self, src_tokens: str, tgt_tokens: str) -> list[tuple[int, int]]:
        if not src_tokens or not tgt_tokens:
            return []
        alignments = self._aligner.get_word_aligns(src_tokens, tgt_tokens)
        return sorted(alignments.get(self._matching_key, []))

    def align_batch(self, pairs: list[tuple[list, list]]) -> list[list[tuple[int, int]]]:
        return [self.align(src, tgt) for src, tgt in pairs]



@dataclass
class GlossaryEntry:
    source: str
    target: str
    frequency: int
    consistency: float
    dice: float
    alternatives: dict[str, int] = field(default_factory=dict)


class PhrasePairExtractor:
    """优化后的 PhrasePairExtractor：基于 CandidateGenerator 遍历"""
    def __init__(self, max_phrase_len: int = 5):
        self.max_phrase_len = max_phrase_len
        self.generator = CandidateGenerator()
        self._STOPWORDS = ['的', '了', '在', '是', '和', '与', '也', '都', '就', '但', '而', '这', '那', '个']

    def _is_stopword(self, tokens: list[str]) -> bool:
        if len(tokens) > 1: return False
        return tokens[0].lower() in self._STOPWORDS
    
    def _clean_phrase(self, tokens: list[str]) -> str:
        """
        对 token 列表进行清洗：
        1. 合并空格
        2. 去除首尾空白和标点
        """
        phrase = ' '.join(tokens).strip()
        # 去除首尾的标点符号 (例如: ", camera module ." -> "camera module")
        phrase = re.sub(r'^[^\w\s]+|[^\w\s]+$', '', phrase)
        # 将中间多个空格合并为一个
        phrase = re.sub(r'\s+', ' ', phrase)
        return phrase

    def extract(
        self,
        src_tokens: list[str],
        tgt_tokens: list[str],
        alignments: list[tuple[int, int]],
        src_pos: list[str] | None = None,
        tgt_pos: list[str] | None = None,
        src_lemmas: list[str] | None = None,
        tgt_lemmas: list[str] | None = None,
    ) -> list[tuple[str, str]]:
        if not alignments:
            return []

        src_to_tgt = defaultdict(set)
        tgt_to_src = defaultdict(set)
        for si, ti in alignments:
            src_to_tgt[si].add(ti)
            tgt_to_src[ti].add(si)

        results = set()
        candidate_windows = self.generator.generate(src_tokens, src_pos, self.max_phrase_len)
        for start, end in candidate_windows:
            tgt_indices = set()
            for si in range(start, end):
                tgt_indices.update(src_to_tgt.get(si, set()))

            if not tgt_indices:
                continue
            tgt_min, tgt_max = min(tgt_indices), max(tgt_indices)

            consistent = True
            for ti in range(tgt_min, tgt_max + 1):
                for si in tgt_to_src.get(ti, set()):
                    if si < start or si >= end:
                        consistent = False
                        break
                if not consistent:
                    break
            if not consistent:
                continue

            if src_pos is not None and POSFilter.is_function_word_only(src_pos, start, end):
                continue
            if tgt_pos is not None and POSFilter.is_function_word_only(tgt_pos, tgt_min, tgt_max + 1):
                continue

            src_phrase_tokens = src_tokens[start:end]
            tgt_phrase_tokens = tgt_tokens[tgt_min:tgt_max + 1]

            if src_pos is not None:
                src_content_count = sum(1 for p in src_pos[start:end] if p in POSFilter.CONTENT_TAGS)
                if src_content_count == 0 or src_content_count / len(src_phrase_tokens) < 0.6:
                    continue
            if tgt_pos is not None:
                tgt_content_count = sum(1 for p in tgt_pos[tgt_min:tgt_max + 1] if p in POSFilter.CONTENT_TAGS)
                if tgt_content_count == 0 or tgt_content_count / len(tgt_phrase_tokens) < 0.6:
                    continue

            src_cleaned = self._clean_phrase(src_phrase_tokens)
            tgt_cleaned = self._clean_phrase(tgt_phrase_tokens)
            if len(src_cleaned) < 2 or len(tgt_cleaned) < 2:
                continue
            if re.search(r'[^\w\s]', src_cleaned) or re.search(r'[^\w\s]', tgt_cleaned):
                continue

            if self._is_stopword(src_phrase_tokens) or self._is_stopword(tgt_phrase_tokens):
                continue

            results.add((src_cleaned, tgt_cleaned))

            # src_lemma_slice = src_lemmas[start:end] if src_lemmas else None
            # tgt_lemma_slice = tgt_lemmas[tgt_min:tgt_max + 1] if tgt_lemmas else None
            # src_norm = PhraseNormalizer.normalize(src_cleaned, src_lang, src_phrase_tokens, src_lemma_slice)
            # tgt_norm = PhraseNormalizer.normalize(tgt_cleaned, tgt_lang, tgt_phrase_tokens, tgt_lemma_slice)
            # if not src_norm or not tgt_norm:
            #     continue

            # results.add((src_norm, tgt_norm))

        return list(results)


class GlossaryBuilder:
    def __init__(
        self,
        src_lang: str = 'en',
        tgt_lang: str = 'zh',
        max_phrase_len: int = 5,
        aligner: WordAligner | None = None,
    ):
        self._aligner = aligner or WordAligner.get_instance()
        self._extractor = PhrasePairExtractor(max_phrase_len=max_phrase_len)
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        self._phrase_pairs: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def add_pairs(self, pairs: list[tuple[str, str]]):
        if not pairs:
            return

        valid = [(s, t) for s, t in pairs if s and s.strip() and t and t.strip()]
        if not valid:
            return

        tok_pairs = [
            (Tokenizer.tokenize(s, self.src_lang), Tokenizer.tokenize(t, self.tgt_lang))
            for s, t in valid
        ]
        tok_pairs = [(s, t) for s, t in tok_pairs if s and t]
        if not tok_pairs:
            return
        
        filtered_pairs, filtered_pos, filtered_lemmas = [], [], []
        for tok_src_tokens, tok_tgt_tokens in tok_pairs:
            src_pos, src_lemmas = POSFilter.tag_and_lemmatize(tok_src_tokens, self.src_lang)
            if src_pos is not None and not any(t in POSFilter.CONTENT_TAGS for t in src_pos):
                continue
            tgt_pos, tgt_lemmas = POSFilter.tag_and_lemmatize(tok_tgt_tokens, self.tgt_lang)
            filtered_pairs.append((tok_src_tokens, tok_tgt_tokens))
            filtered_pos.append((src_pos, tgt_pos))
            filtered_lemmas.append((src_lemmas, tgt_lemmas))
        if not filtered_pairs:
            return

        all_alignments = self._aligner.align_batch(filtered_pairs)
        for (src_words, tgt_words), alignments, (src_pos, tgt_pos) in zip(filtered_pairs, all_alignments, filtered_pos):
            phrase_pairs = self._extractor.extract(src_words, tgt_words, alignments, src_pos, tgt_pos)
            for src_p, tgt_p in phrase_pairs:
                self._phrase_pairs[src_p][tgt_p] += 1

    def build(
        self,
        min_freq: int = 1,
        min_consistency: float = 0.34,
        min_dice: float = 0.15,
        filter_common_terms: bool = True,
    ) -> list[GlossaryEntry]:
        tgt_totals: dict[str, int] = defaultdict(int)
        for tgt_counts in self._phrase_pairs.values():
            for tgt, cnt in tgt_counts.items():
                tgt_totals[tgt] += cnt

        entries = []
        for src_phrase, tgt_counts in self._phrase_pairs.items():
            src_total = sum(tgt_counts.values())
            if src_total < min_freq:
                continue
            best_tgt = max(tgt_counts, key=tgt_counts.get)
            co_occur = tgt_counts[best_tgt]
            tgt_total = tgt_totals[best_tgt]

            p_tgt_given_src = co_occur / src_total
            p_src_given_tgt = co_occur / tgt_total if tgt_total else 0.0
            dice = (2 * co_occur) / (src_total + tgt_total) if (src_total + tgt_total) else 0.0

            # if p_tgt_given_src < min_consistency:
            #     continue
            # if dice < min_dice:
            #     continue

            # if filter_common_terms:
            #     src_common = TermhoodScorer.is_too_common(src_phrase, self.src_lang)
            #     tgt_common = TermhoodScorer.is_too_common(best_tgt, self.tgt_lang)
            #     if src_common and tgt_common:
            #         continue

            entries.append(
                GlossaryEntry(
                    source=src_phrase,
                    target=best_tgt,
                    frequency=src_total,
                    consistency=round(p_tgt_given_src, 3),
                    dice=round(dice, 3),
                    alternatives={k: v for k, v in tgt_counts.items() if k != best_tgt},
                )
            )
        return sorted(entries, key=lambda x: (x.dice, x.frequency), reverse=True)

    def clear(self):
        self._phrase_pairs.clear()
