type TokenUsage = Record<string, unknown>;

type ContextMessage = {
	role?: string;
	parentId?: string | null;
	contextUsage?: ContextUsageSnapshot;
	usage?: TokenUsage;
};

export type ContextUsageSnapshot = {
	used_tokens?: number;
	input_tokens?: number;
	output_tokens?: number;
	limit_tokens?: number;
	limit_source?: string;
	threshold_tokens?: number;
	threshold_percent?: number;
	// Read-only compatibility marker for estimates saved by v3.9.35.
	estimated?: boolean;
	compacted?: boolean;
	compaction_enabled?: boolean;
	hard_truncation?: boolean;
};

export type ContextUsageDetails = {
	available: boolean;
	usedTokens: number;
	inputTokens: number;
	outputTokens: number;
	limitTokens: number | null;
	percent: number | null;
	thresholdTokens: number | null;
	thresholdPercent: number | null;
	compacted: boolean;
	compactionEnabled: boolean;
	hardTruncation: boolean;
	level: 'unknown' | 'normal' | 'warning' | 'high' | 'critical';
};

type ContextUsageOptions = {
	history?: {
		currentId?: string | null;
		messages?: Record<string, ContextMessage>;
	};
	model?: unknown;
	requestParams?: Record<string, unknown>;
};

const asRecord = (value: unknown): Record<string, unknown> =>
	typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {};

const toNonnegativeNumber = (value: unknown): number | null => {
	const parsed = typeof value === 'number' ? value : Number(value);
	return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
};

const toPositiveNumber = (value: unknown): number | null => {
	const parsed = toNonnegativeNumber(value);
	return parsed !== null && parsed > 0 ? parsed : null;
};

const findLatestContextMessage = (history: ContextUsageOptions['history']) => {
	const messages = history?.messages ?? {};
	let messageId = history?.currentId ?? null;
	const visited = new Set<string>();

	while (messageId && !visited.has(messageId)) {
		visited.add(messageId);
		const message = messages[messageId];
		if (!message) break;
		const hasExactSnapshot = message.contextUsage && message.contextUsage.estimated !== true;
		if (message.role === 'assistant' && (hasExactSnapshot || message.usage)) {
			return message;
		}
		messageId = message.parentId ?? null;
	}

	return null;
};

const resolveModelContextLimit = (model: unknown, requestParams: Record<string, unknown>) => {
	const modelRecord = asRecord(model);
	const modelInfo = asRecord(modelRecord.info);
	const modelParams = asRecord(modelInfo.params);
	const modelMeta = asRecord(modelInfo.meta);
	const providerMeta = asRecord(asRecord(modelRecord.openai).meta);
	const baseMeta = asRecord(modelRecord.meta);
	const candidates = [
		requestParams?.num_ctx,
		requestParams?.n_ctx,
		requestParams?.ctx_size,
		requestParams?.context_length,
		requestParams?.context_size,
		modelParams?.num_ctx,
		modelParams?.n_ctx,
		modelParams?.ctx_size,
		modelParams?.context_length,
		modelParams?.context_size,
		baseMeta?.n_ctx,
		baseMeta?.context_length,
		baseMeta?.context_size,
		modelMeta?.n_ctx,
		modelMeta?.context_length,
		modelMeta?.context_size,
		providerMeta?.n_ctx,
		providerMeta?.context_length,
		providerMeta?.context_size,
		modelRecord.n_ctx,
		modelRecord.context_length
	];

	for (const value of candidates) {
		const parsed = toPositiveNumber(value);
		if (parsed !== null) return parsed;
	}

	return null;
};

const normalizeLegacyUsage = (usage: TokenUsage | null | undefined) => {
	if (!usage) return null;
	const promptTokens = toNonnegativeNumber(
		usage.input_tokens ?? usage.prompt_tokens ?? usage.prompt_eval_count ?? usage.prompt_n
	);
	const cacheTokens = toNonnegativeNumber(usage.cache_n);
	const llamaPromptTokens = toNonnegativeNumber(usage.prompt_n);
	const hasLlamaCacheUsage = cacheTokens !== null && llamaPromptTokens !== null;
	const inputTokens = hasLlamaCacheUsage ? cacheTokens + llamaPromptTokens : promptTokens;
	const outputTokens = toNonnegativeNumber(
		usage.output_tokens ?? usage.completion_tokens ?? usage.eval_count ?? usage.predicted_n
	);
	const totalTokens = toNonnegativeNumber(usage.total_tokens);
	if (inputTokens === null && outputTokens === null && totalTokens === null) return null;

	const input = inputTokens ?? 0;
	const output = outputTokens ?? 0;
	return {
		inputTokens: input,
		outputTokens: output,
		usedTokens: hasLlamaCacheUsage ? input + output : (totalTokens ?? input + output)
	};
};

const resolveLevel = (
	percent: number | null,
	thresholdPercent: number | null
): ContextUsageDetails['level'] => {
	if (percent === null) return 'unknown';
	if (percent >= 90) return 'critical';
	if (thresholdPercent !== null && percent >= thresholdPercent) return 'high';
	if (percent >= Math.min(65, (thresholdPercent ?? 75) * 0.85)) return 'warning';
	return 'normal';
};

export const resolveContextUsage = ({
	history,
	model,
	requestParams = {}
}: ContextUsageOptions): ContextUsageDetails => {
	const message = findLatestContextMessage(history);
	const snapshot = (
		message?.contextUsage?.estimated === true ? null : (message?.contextUsage ?? null)
	) as ContextUsageSnapshot | null;
	const legacyUsage = snapshot ? null : normalizeLegacyUsage(message?.usage);

	const snapshotUsed = toNonnegativeNumber(snapshot?.used_tokens);
	const snapshotInput = toNonnegativeNumber(snapshot?.input_tokens);
	const snapshotOutput = toNonnegativeNumber(snapshot?.output_tokens);
	const available = snapshotUsed !== null || legacyUsage !== null;
	const inputTokens = snapshotInput ?? legacyUsage?.inputTokens ?? 0;
	const outputTokens = snapshotOutput ?? legacyUsage?.outputTokens ?? 0;
	const usedTokens = snapshotUsed ?? legacyUsage?.usedTokens ?? 0;
	const limitTokens =
		toPositiveNumber(snapshot?.limit_tokens) ?? resolveModelContextLimit(model, requestParams);
	const thresholdTokens =
		toPositiveNumber(snapshot?.threshold_tokens) ??
		(limitTokens !== null ? Math.round(limitTokens * 0.75) : null);
	const thresholdPercent =
		toPositiveNumber(snapshot?.threshold_percent) ??
		(limitTokens !== null && thresholdTokens !== null
			? (thresholdTokens * 100) / limitTokens
			: null);
	const percent = limitTokens !== null ? (usedTokens * 100) / limitTokens : null;

	return {
		available,
		usedTokens,
		inputTokens,
		outputTokens,
		limitTokens,
		percent,
		thresholdTokens,
		thresholdPercent,
		compacted: Boolean(snapshot?.compacted),
		compactionEnabled: snapshot?.compaction_enabled ?? true,
		hardTruncation: Boolean(snapshot?.hard_truncation),
		level: resolveLevel(percent, thresholdPercent)
	};
};

export const formatCompactTokenCount = (value: number): string => {
	if (value < 1000) return Math.round(value).toString();
	if (value < 1_000_000) {
		const compact = value / 1000;
		return `${compact >= 10 ? compact.toFixed(0) : compact.toFixed(1)}K`;
	}
	const compact = value / 1_000_000;
	return `${compact >= 10 ? compact.toFixed(0) : compact.toFixed(1)}M`;
};
