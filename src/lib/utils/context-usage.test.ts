import { describe, expect, it } from 'vitest';

import { formatCompactTokenCount, resolveContextUsage } from './context-usage';

const model = {
	id: 'local-model',
	info: {
		params: { num_ctx: 32768 },
		meta: {}
	}
};

describe('resolveContextUsage', () => {
	it('uses the persisted post-compaction snapshot', () => {
		const details = resolveContextUsage({
			history: {
				currentId: 'assistant-1',
				messages: {
					'assistant-1': {
						role: 'assistant',
						contextUsage: {
							used_tokens: 8200,
							input_tokens: 7000,
							output_tokens: 1200,
							limit_tokens: 32768,
							threshold_tokens: 24576,
							threshold_percent: 75,
							compacted: true,
							compaction_enabled: true
						}
					}
				}
			},
			model
		});

		expect(details.usedTokens).toBe(8200);
		expect(details.percent).toBeCloseTo(25.02, 1);
		expect(details.compacted).toBe(true);
	});

	it('falls back to legacy llama.cpp usage and the effective request context', () => {
		const details = resolveContextUsage({
			history: {
				currentId: 'assistant-1',
				messages: {
					'assistant-1': {
						role: 'assistant',
						usage: { prompt_n: 6000, predicted_n: 500 }
					}
				}
			},
			model,
			requestParams: { num_ctx: 16384 }
		});

		expect(details.usedTokens).toBe(6500);
		expect(details.limitTokens).toBe(16384);
		expect(details.available).toBe(true);
	});

	it('includes llama.cpp reused prompt cache in legacy exact usage', () => {
		const details = resolveContextUsage({
			history: {
				currentId: 'assistant-1',
				messages: {
					'assistant-1': {
						role: 'assistant',
						usage: {
							cache_n: 17905,
							prompt_n: 1316,
							predicted_n: 168,
							input_tokens: 1316,
							output_tokens: 168,
							total_tokens: 1484
						}
					}
				}
			},
			model: {
				id: 'local-model',
				meta: { n_ctx: 20224 }
			}
		});

		expect(details.inputTokens).toBe(19221);
		expect(details.outputTokens).toBe(168);
		expect(details.usedTokens).toBe(19389);
		expect(details.limitTokens).toBe(20224);
	});

	it('walks up the active branch while a new response is pending', () => {
		const details = resolveContextUsage({
			history: {
				currentId: 'assistant-pending',
				messages: {
					'assistant-pending': {
						role: 'assistant',
						parentId: 'user-2'
					},
					'user-2': {
						role: 'user',
						parentId: 'assistant-1'
					},
					'assistant-1': {
						role: 'assistant',
						usage: { input_tokens: 1000, output_tokens: 200 }
					}
				}
			},
			model
		});

		expect(details.usedTokens).toBe(1200);
	});

	it('ignores a pending estimate and keeps the latest exact model usage', () => {
		const details = resolveContextUsage({
			history: {
				currentId: 'assistant-pending',
				messages: {
					'assistant-pending': {
						role: 'assistant',
						parentId: 'user-2',
						contextUsage: {
							used_tokens: 4000,
							limit_tokens: 20224,
							estimated: true
						}
					},
					'user-2': {
						role: 'user',
						parentId: 'assistant-1'
					},
					'assistant-1': {
						role: 'assistant',
						contextUsage: {
							used_tokens: 19389,
							input_tokens: 19221,
							output_tokens: 168,
							limit_tokens: 20224
						}
					}
				}
			},
			model
		});

		expect(details.usedTokens).toBe(19389);
	});
});

describe('formatCompactTokenCount', () => {
	it('formats compact token values without unnecessary precision', () => {
		expect(formatCompactTokenCount(980)).toBe('980');
		expect(formatCompactTokenCount(12400)).toBe('12K');
		expect(formatCompactTokenCount(1_250_000)).toBe('1.3M');
	});
});
