import logging
import numpy as np
from dataclasses import dataclass, field


logger = logging.getLogger('bilingual_align')


@dataclass
class AlignmentConfig:
    """集中管理所有对齐参数"""

    max_align: int = 4
    min_score: float = 0.35
    empty_align_base_sim: float = 0.4
    empty_align_scale: float = 2.0
    fallback_score: float = 0.20
    debug_align: bool = False

    def __post_init__(self):
        # 参数校验
        if not 0 < self.min_score < 1:
            raise ValueError('min_score 必须在 (0, 1) 之间')
        if not 0 < self.fallback_score < 1:
            raise ValueError('fallback_score 必须在 (0, 1) 之间')
        if self.fallback_score > self.min_score:
            raise ValueError('fallback_score 应小于等于 min_score')


@dataclass
class AlignContext:
    """对齐上下文，避免重复传递参数"""

    para_index: int = 0
    para_source: str = ''
    para_target: str | dict = ''


@dataclass
class AlignedPair:
    source: str
    target: str
    src_indices: list
    tgt_indices: list
    score: float
    para_index: int = 0
    para_source: str = ''
    para_target: str = ''
    extra_targets: dict = field(default_factory=dict)
    fallback: bool = False  # 标记是否经过"兜底复活"
    is_empty_aligned: bool = False  # 标记是否判定为真正的空对齐（漏译/多余，没有有效配对）
    is_low_confidence: bool = False  # 标记是否"DP配了对，但相似度不够，需复活"


@dataclass
class SentBlock:
    sents: list[str]
    embs: np.ndarray
    prefix_sums: np.ndarray = field(init=False)

    def __post_init__(self):
        """初始化时计算前缀和，避免 DP 中重复计算"""
        n = len(self.embs)
        dim = self.embs.shape[1] if self.embs.ndim > 1 else 1

        # 预分配内存，避免逐行累加
        prefix = np.zeros((n + 1, dim), dtype=self.embs.dtype)
        if self.embs.ndim > 1:
            np.cumsum(self.embs, axis=0, out=prefix[1:])
        else:
            np.cumsum(self.embs, out=prefix[1:])
        self.prefix_sums = prefix

    def get_mean_embedding(self, start: int, end: int) -> np.ndarray:
        """快速计算 [start, end) 范围内的平均 embedding"""
        count = end - start
        if count == 0:
            return np.zeros(self.embs.shape[1])
        return (self.prefix_sums[end] - self.prefix_sums[start]) / count


class DPAlignMixin:
    def __init__(self, config: AlignmentConfig | None = None):
        self.config = config or AlignmentConfig()
        self._dp_steps_cache: list[tuple[int, int]] | None = None
        self._sim_cache: dict = {}

    @property
    def dp_steps(self) -> list[tuple[int, int]]:
        """缓存 DP 转移步骤，避免重复生成"""
        if self._dp_steps_cache is None:
            steps = [
                (si, ti) for si in range(1, self.config.max_align + 1) for ti in range(1, self.config.max_align + 1)
            ]
            steps += [(si, 0) for si in range(1, self.config.max_align + 1)]
            steps += [(0, ti) for ti in range(1, self.config.max_align + 1)]
            self._dp_steps_cache = steps
        return self._dp_steps_cache

    def _create_aligned_pair(
        self, source: str, target: str, src_indices: list, tgt_indices: list, score: float, ctx: AlignContext, **kwargs
    ) -> AlignedPair:
        return AlignedPair(
            source=source,
            target=target,
            src_indices=src_indices,
            tgt_indices=tgt_indices,
            score=score,
            para_index=ctx.para_index,
            para_source=ctx.para_source,
            para_target=ctx.para_target,
            **kwargs,
        )

    def _log(self, msg, *args):
        if self.config.debug_align:
            logger.info(msg, *args)

    def _dp_align(
        self,
        src: SentBlock,
        tgt: SentBlock,
        ctx: AlignContext,
    ) -> list:
        m, n = len(src.sents), len(tgt.sents)
        self._log('=== _dp_align para_index=%s, m=%d(src句数), n=%d(tgt句数) ===', ctx.para_index, m, n)
        self._log('src.sents=%s', src.sents)
        self._log('tgt.sents=%s', tgt.sents)

        if m == 0 and n == 0:
            return []

        if n == 0:
            self._log('n==0，全部src判定为空对齐（tgt整段为空）')
            return [
                self._create_aligned_pair(
                    source=s,
                    target='',
                    src_indices=[k],
                    tgt_indices=[],
                    score=0.0,
                    ctx=ctx,
                    is_empty_aligned=True,
                )
                for k, s in enumerate(src.sents)
                if s.strip()
            ]
        if m == 0:
            self._log('m==0，全部tgt判定为空对齐（src整段为空）')
            return [
                self._create_aligned_pair(
                    source='',
                    target=t,
                    src_indices=[],
                    tgt_indices=[k],
                    score=0.0,
                    ctx=ctx,
                    is_empty_aligned=True,
                )
                for k, t in enumerate(tgt.sents)
                if t.strip()
            ]

        sim_matrix = src.embs @ tgt.embs.T  # (m, n)
        src_max_sim = sim_matrix.max(axis=1)  # (m,)
        tgt_max_sim = sim_matrix.max(axis=0)  # (n,)

        self._log('sim_matrix:\n%s', np.round(sim_matrix, 3))
        self._log('src_max_sim(每句src在tgt里能找到的最高相似度)=%s', np.round(src_max_sim, 3))
        self._log('tgt_max_sim(每句tgt在src里能找到的最高相似度)=%s', np.round(tgt_max_sim, 3))

        NEG_INF = float('-inf')
        dp = [[NEG_INF] * (n + 1) for _ in range(m + 1)]
        back = [[None] * (n + 1) for _ in range(m + 1)]
        dp[0][0] = 0.0
        back[0][0] = (0, 0, 0, 0, 0.0, False)

        for i in range(m + 1):
            for j in range(n + 1):
                if dp[i][j] == NEG_INF:
                    continue
                cur = dp[i][j]

                for si, ti in self.dp_steps:
                    ni, nj = i + si, j + ti
                    if ni > m or nj > n:
                        continue
                    if si == 0 and ti == 0:
                        continue

                    is_empty = False
                    if si > 0 and ti > 0:
                        s_vec = src.get_mean_embedding(i, ni)
                        t_vec = tgt.get_mean_embedding(j, nj)
                        s_norm = np.linalg.norm(s_vec)
                        t_norm = np.linalg.norm(t_vec)
                        sim = 0.0 if s_norm == 0 or t_norm == 0 else float(np.dot(s_vec / s_norm, t_vec / t_norm))
                        penalty = 0.1 * (si - 1) + 0.1 * (ti - 1)
                    elif si > 0:
                        peak_sim = src_max_sim[i : i + si].max()
                        sim = 0.0
                        penalty = max(0.0, peak_sim - self.config.empty_align_base_sim) * self.config.empty_align_scale
                        is_empty = True
                    else:
                        peak_sim = tgt_max_sim[j : j + ti].max()
                        sim = 0.0
                        penalty = max(0.0, peak_sim - self.config.empty_align_base_sim) * self.config.empty_align_scale
                        is_empty = True

                    total = cur + sim - penalty
                    if total > dp[ni][nj]:
                        dp[ni][nj] = total
                        back[ni][nj] = (i, j, si, ti, sim, is_empty)

        # 回溯提取对齐对
        pairs = self._backtrack(src, tgt, back, m, n, ctx)

        # 后处理
        pairs = self._fallback_recover(pairs, src, tgt, sim_matrix, ctx)
        pairs = self._merge_orphan_targets(pairs, sim_matrix)

        if self.config.debug_align:
            self.visualize_alignment(sim_matrix, pairs, ctx.para_index)
        return pairs

    def _backtrack(
        self,
        src: SentBlock,
        tgt: SentBlock,
        back: list,
        m: int,
        n: int,
        ctx: AlignContext,
    ) -> list[AlignedPair]:
        """从 DP 表回溯，提取对齐对"""
        pairs, i, j = [], m, n

        while i > 0 or j > 0:
            node = back[i][j]
            if node is None:
                # 兜底处理剩余句子
                for k in range(i - 1, -1, -1):
                    chunk = src.sents[k].strip()
                    if chunk:
                        pairs.append(
                            self._create_aligned_pair(
                                source=chunk,
                                target='',
                                src_indices=[k],
                                tgt_indices=[],
                                score=0.0,
                                ctx=ctx,
                                is_empty_aligned=True,
                            )
                        )
                for k in range(j - 1, -1, -1):
                    chunk = tgt.sents[k].strip()
                    if chunk:
                        pairs.append(
                            self._create_aligned_pair(
                                source='',
                                target=chunk,
                                src_indices=[],
                                tgt_indices=[k],
                                score=0.0,
                                ctx=ctx,
                                is_empty_aligned=True,
                            )
                        )
                break

            pi, pj, si, ti, sim, is_empty = node
            if pi == i and pj == j:
                break

            src_chunk = ' '.join(src.sents[pi : pi + si]).strip()
            tgt_chunk = ' '.join(tgt.sents[pj : pj + ti]).strip()

            if si > 0 and ti > 0:
                low_conf = sim < self.config.min_score
                pairs.append(
                    self._create_aligned_pair(
                        source=src_chunk,
                        target=tgt_chunk,
                        src_indices=list(range(pi, pi + si)),
                        tgt_indices=list(range(pj, pj + ti)),
                        score=round(sim, 4),
                        ctx=ctx,
                        is_low_confidence=low_conf,
                    )
                )
            elif si > 0 and src_chunk:
                pairs.append(
                    self._create_aligned_pair(
                        source=src_chunk,
                        target='',
                        src_indices=list(range(pi, pi + si)),
                        tgt_indices=[],
                        score=0.0,
                        ctx=ctx,
                        is_empty_aligned=True,
                    )
                )
            elif ti > 0 and tgt_chunk:
                pairs.append(
                    self._create_aligned_pair(
                        source='',
                        target=tgt_chunk,
                        src_indices=[],
                        tgt_indices=list(range(pj, pj + ti)),
                        score=0.0,
                        ctx=ctx,
                        is_empty_aligned=True,
                    )
                )

            i, j = pi, pj

        pairs.reverse()
        return pairs

    def _fallback_recover(self, pairs, src, tgt, sim_matrix, ctx):
        used_tgt = set()
        used_src = set()
        for p in pairs:
            used_tgt.update(p.tgt_indices)
            used_src.update(p.src_indices)

        recovered = []
        for p in pairs:
            if p.is_low_confidence and len(p.src_indices) == 1 and len(p.tgt_indices) == 1:
                si, ti = p.src_indices[0], p.tgt_indices[0]
                orig_sim = float(sim_matrix[si, ti])

                row = sim_matrix[si].copy()
                for used in used_tgt:
                    if used != ti:
                        row[used] = -1.0
                best_j = int(row.argmax()) if row.size else -1
                best_sim = float(row[best_j]) if best_j >= 0 else -1.0

                if best_j != ti and best_sim > orig_sim and best_sim >= self.config.fallback_score:
                    used_tgt.discard(ti)
                    used_tgt.add(best_j)
                    recovered.append(
                        self._create_aligned_pair(
                            source=p.source,
                            target=tgt.sents[best_j].strip(),
                            src_indices=p.src_indices,
                            tgt_indices=[best_j],
                            score=round(best_sim, 4),
                            ctx=ctx,
                            fallback=True,
                        )
                    )
                elif orig_sim >= self.config.fallback_score:
                    p.is_low_confidence = False
                    recovered.append(p)
                else:
                    used_tgt.discard(ti)
                    recovered.append(
                        self._create_aligned_pair(
                            source=p.source,
                            target='',
                            src_indices=p.src_indices,
                            tgt_indices=[],
                            score=0.0,
                            ctx=ctx,
                            is_empty_aligned=True,
                        )
                    )
                continue

            elif p.is_empty_aligned:
                if p.src_indices and not p.tgt_indices and len(p.src_indices) == 1:
                    recovered.append(self._match_orphan_generic(p, src, tgt, sim_matrix, used_src, used_tgt, True, ctx))
                elif p.tgt_indices and not p.src_indices and len(p.tgt_indices) == 1:
                    recovered.append(
                        self._match_orphan_generic(p, src, tgt, sim_matrix, used_src, used_tgt, False, ctx)
                    )
                else:
                    recovered.append(p)
            else:
                recovered.append(p)

        return recovered

    def _match_orphan_generic(
        self,
        orphan_pair: AlignedPair,
        src: SentBlock,
        tgt: SentBlock,
        sim_matrix: np.ndarray,
        used_src: set,
        used_tgt: set,
        is_src_orphan: bool,
        ctx: AlignContext,
    ) -> AlignedPair:
        """统一的孤儿匹配逻辑（通用 src 和 tgt 孤儿）"""
        if is_src_orphan:
            si = orphan_pair.src_indices[0]
            row = sim_matrix[si].copy()
            row[list(used_tgt)] = -1.0
            best_j = int(row.argmax()) if row.size else -1
            best_sim = float(row[best_j]) if best_j >= 0 else -1.0

            if best_sim >= self.config.fallback_score:
                used_tgt.add(best_j)
                return self._create_aligned_pair(
                    source=orphan_pair.source,
                    target=tgt.sents[best_j].strip(),
                    src_indices=orphan_pair.src_indices,
                    tgt_indices=[best_j],
                    score=round(best_sim, 4),
                    ctx=ctx,
                    fallback=True,
                    is_empty_aligned=False,
                )
        else:
            ti = orphan_pair.tgt_indices[0]
            col = sim_matrix[:, ti].copy()
            col[list(used_src)] = -1.0
            best_i = int(col.argmax()) if col.size else -1
            best_sim = float(col[best_i]) if best_i >= 0 else -1.0

            if best_sim >= self.config.fallback_score:
                used_src.add(best_i)
                return self._create_aligned_pair(
                    source=src.sents[best_i].strip(),
                    target=orphan_pair.target,
                    src_indices=[best_i],
                    tgt_indices=orphan_pair.tgt_indices,
                    score=round(best_sim, 4),
                    ctx=ctx,
                    fallback=True,
                    is_empty_aligned=False,
                )

        return orphan_pair

    def _merge_orphan_targets(self, pairs, sim_matrix):
        merged = []
        i = 0
        while i < len(pairs):
            p = pairs[i]

            if p.is_empty_aligned and not p.src_indices and p.tgt_indices:
                ti = p.tgt_indices[0]
                left = merged[-1] if merged else None
                right = pairs[i + 1] if i + 1 < len(pairs) else None

                candidates = []
                if left is not None and left.src_indices and not left.is_empty_aligned:
                    avg_sim = float(np.mean([sim_matrix[si, ti] for si in left.src_indices]))
                    candidates.append(('left', avg_sim, left))
                if right is not None and right.src_indices and not right.is_empty_aligned:
                    avg_sim = float(np.mean([sim_matrix[si, ti] for si in right.src_indices]))
                    candidates.append(('right', avg_sim, right))

                if candidates:
                    side, best_sim, neighbor = max(candidates, key=lambda x: x[1])
                    if best_sim >= self.config.fallback_score:
                        self._log(
                            '[孤儿target合并] tgt[%d]=%r 并入%s邻居(src=%r), avg_sim=%.4f',
                            ti,
                            p.target,
                            side,
                            neighbor.source,
                            best_sim,
                        )
                        if side == 'left':
                            neighbor.target = (neighbor.target + ' ' + p.target).strip()
                            neighbor.tgt_indices = neighbor.tgt_indices + p.tgt_indices
                        else:
                            neighbor.target = (p.target + ' ' + neighbor.target).strip()
                            neighbor.tgt_indices = p.tgt_indices + neighbor.tgt_indices
                        i += 1
                        continue

            merged.append(p)
            i += 1
        return merged

    def visualize_alignment(self, sim_matrix, pairs, para_index=0):
        import matplotlib.pyplot as plt
        import seaborn as sns

        plt.figure(figsize=(12, 8))

        # 画热力图
        ax = sns.heatmap(
            sim_matrix,
            annot=True,  # 显示具体相似度数字
            fmt='.2f',
            cmap='YlOrRd',  # 黄色→橙色→红色，越红越相似
            cbar_kws={'label': '相似度'},
        )

        plt.title(f'段落 {para_index} 对齐热力图\n黄色=低相似  红色=高相似')
        plt.xlabel('目标语言句子 (Target)')
        plt.ylabel('源语言句子 (Source)')

        # 在图上画出 DP 选择的匹配路径
        for p in pairs:
            if p.src_indices and p.tgt_indices:
                src_start = p.src_indices[0]
                src_end = p.src_indices[-1]
                tgt_start = p.tgt_indices[0]
                tgt_end = p.tgt_indices[-1]

                # 画连接线
                plt.plot(
                    [tgt_start + 0.5, tgt_end + 0.5], [src_start + 0.5, src_end + 0.5], 'b-', linewidth=2.5, alpha=0.8
                )  # 蓝色实线

        plt.tight_layout()
        plt.savefig(f'align_visual_{para_index}.png', dpi=200, bbox_inches='tight')
        plt.show()
