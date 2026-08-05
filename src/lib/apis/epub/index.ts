import { WEBUI_API_BASE_URL } from '$lib/constants';

export type EpubBook = {
	book_id: string;
	title: string;
	current_version_id: string | null;
	current_version_status: string | null;
	created_at?: number;
	updated_at?: number;
};

export type EpubVersion = {
	version_id: string;
	book_id: string;
	epub_sha256: string;
	source_locator: string | null;
	status: string;
	parser_version?: string;
	created_at?: number;
	ready_at?: number | null;
	failure_reason?: string | null;
};

export type EpubBookDetail = EpubBook & {
	versions: EpubVersion[];
};

export type EpubPassage = {
	passage_id: string;
	version_id?: string;
	content: string;
	content_sha256?: string;
	content_kind?: string;
	toc_path?: string[];
	spine_index?: number;
	ordinal?: number;
};

export type EpubPassagePage = {
	version_id: string;
	total: number;
	offset: number;
	items: EpubPassage[];
};

export type EpubExcerpt = {
	content: string;
	start_codepoint: number;
	end_codepoint: number;
};

export type EpubSearchHit = {
	passage_id: string;
	book_title: string;
	toc_path: string[];
	content: string;
	content_sha256: string;
	matched_concepts: string[];
	provenance: string[];
	excerpt: EpubExcerpt;
	score: number | null;
};

export type EpubDegradedState = {
	component: string;
	reason?: string | null;
};

export type EpubSearchResponse = {
	query: string;
	resolved_concepts: string[];
	graph_total: number;
	graph_offset: number;
	graph_results: EpubSearchHit[];
	vector_results: EpubSearchHit[];
	fused_results: EpubSearchHit[];
	degraded: EpubDegradedState[];
};

export type BatchDraftInput = {
	version_id: string;
	profile_name: string;
	prompt_profile?: string;
	is_sample: boolean;
	sample_limit: number;
};

export type BatchDraft = {
	batch_job_id: string;
	item_count: number;
	status: string;
	prompt_profile?: string;
	job_kind?: 'CONCEPT_MENTIONS' | 'SECTION_GRAPH';
	is_sample?: boolean;
};

export type SectionGraphBatchDraftInput = Omit<BatchDraftInput, 'prompt_profile'>;

export type BatchStatus = Record<string, string | number | boolean | null | undefined>;

/**
 * Why an item failed, as counts and flags only. A failed item stores no model
 * output, so this is the only tunable signal a prompt author has. It is
 * validated server-side on write and again on read: no source text, evidence,
 * anchor, prompt, model output or raw provider error can appear here.
 */
export type EpubBatchItemFailureDiagnostics = {
	/** Stable failure class, e.g. `EVIDENCE_ABSENT`, `EVIDENCE_AMBIGUOUS`. */
	reason: string;
	/** Position of the concept/mention within the model response. */
	concept_index?: number;
	concept_count?: number;
	mention_index?: number;
	mention_count?: number;
	passage_codepoints?: number;
	evidence_codepoints?: number;
	/** How many times the literal evidence occurs in the immutable passage. */
	occurrence_count?: number;
	/** Whether the mention used the anchored (v4) mention shape. */
	has_anchors?: boolean;
	anchor_before_codepoints?: number;
	anchor_after_codepoints?: number;
	/** Occurrences still selected after context-anchor filtering. */
	anchored_candidate_count?: number;
	direct_offsets_in_range?: boolean;
	direct_is_exact?: boolean;
};

export type EpubBatchItemSummary = {
	batch_item_id: string;
	passage_id: string;
	custom_id: string;
	status: string;
	attempt_count: number;
	has_response: boolean;
	has_error: boolean;
	failure_diagnostics: EpubBatchItemFailureDiagnostics | null;
	updated_at: string | null;
};

/** Lifecycle-only operational history. Prompts, model output and raw errors are excluded. */
export type EpubBatchSummary = {
	batch_job_id: string;
	version_id: string;
	provider: string;
	provider_job_id: string | null;
	profile_name: string;
	job_kind: 'CONCEPT_MENTIONS' | 'SECTION_GRAPH';
	status: string;
	is_sample: boolean;
	submitted_at: string | null;
	completed_at: string | null;
	created_at: string | null;
	updated_at: string | null;
	has_error: boolean;
	/** A terminal provider job whose output/error files must be polled again. */
	results_pending_retrieval: boolean;
	item_count: number;
	item_status_counts: Record<string, number>;
	/** Failed items grouped by failure class; always sums to the FAILED count. */
	item_failure_reason_counts: Record<string, number>;
	items?: EpubBatchItemSummary[];
};

export type EpubBatchPage = { total: number; offset: number; items: EpubBatchSummary[] };
export type EpubBatchRecovery = {
	recovered: Array<{ job_id: string; state: string; ingested: number; failed: number }>;
	skipped: Array<{ job_id: string; provider: string; reason: string }>;
};

export type LocalCalibrationInput = {
	version_id: string;
	prompt_profile: string;
	sample_limit: number;
};

export type LocalCalibrationReport = {
	mode: 'LOCAL_QWEN';
	prompt_profile: string;
	model: string;
	sample_count: number;
	chapter_count: number;
	valid_items: number;
	invalid_items: number;
	schema_valid_rate: number;
	concept_count: number;
	mention_count: number;
	items: Array<{
		passage_id: string;
		ordinal: number;
		toc_path: string[];
		valid: boolean;
		concept_count: number;
		mention_count: number;
		reason?: string | null;
	}>;
};

export type ConceptInput = {
	canonical_name: string;
	aliases: string[];
	definition: string;
	status: 'PROVISIONAL' | 'APPROVED' | 'REJECTED';
};

export type RelationAssertion = {
	assertion_id: string;
	relation_id: string;
	version_id: string;
	status: 'PROVISIONAL' | 'APPROVED' | 'REJECTED';
	source: 'MODEL' | 'ADMIN';
	predicate: string;
	subject_name: string;
	object_name: string;
	evidence: Array<{
		passage_id: string;
		start_codepoint: number;
		end_codepoint: number;
		evidence: string;
	}>;
};

export type RelationAssertionPage = { total: number; offset: number; items: RelationAssertion[] };

export type SampleBatchReview = {
	sample_batch_job_id: string;
	version_id: string;
	job_kind: 'CONCEPT_MENTIONS' | 'SECTION_GRAPH';
	status: 'APPROVED' | 'REJECTED';
	reviewed_by: string;
	reviewed_at: string;
	batch_status: string;
};

type ApiErrorBody = { detail?: unknown };

const epubUrl = (path: string) => `${WEBUI_API_BASE_URL}/epub${path}`;

const errorDetail = (body: unknown, fallback: string) => {
	if (body && typeof body === 'object' && 'detail' in body) {
		const detail = (body as ApiErrorBody).detail;
		return typeof detail === 'string' ? detail : JSON.stringify(detail);
	}
	return fallback;
};

const request = async <T>(token: string, path: string, init: RequestInit = {}): Promise<T> => {
	const response = await fetch(epubUrl(path), {
		...init,
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`,
			...(init.headers ?? {})
		}
	});

	const text = await response.text();
	let body: unknown = null;
	if (text) {
		try {
			body = JSON.parse(text);
		} catch {
			body = text;
		}
	}
	if (!response.ok) {
		throw new Error(errorDetail(body, response.statusText));
	}
	return body as T;
};

export const getEpubBooks = (token: string) => request<EpubBook[]>(token, '/books');

export const getEpubBook = (token: string, bookId: string) =>
	request<EpubBookDetail>(token, `/books/${encodeURIComponent(bookId)}`);

export const getEpubPassages = (token: string, versionId: string, offset = 0, limit = 50) =>
	request<EpubPassagePage>(
		token,
		`/versions/${encodeURIComponent(versionId)}/passages?offset=${offset}&limit=${limit}`
	);

export const searchEpub = (
	token: string,
	input: { query: string; graph_offset?: number; graph_limit?: number; vector_limit?: number }
) =>
	request<EpubSearchResponse>(token, '/search', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(input)
	});

export const importEpub = (token: string, file: File) => {
	const data = new FormData();
	data.append('file', file);
	return request<Record<string, unknown>>(token, '/admin/import', { method: 'POST', body: data });
};

export const createEpubBatchDraft = (token: string, input: BatchDraftInput) =>
	request<BatchDraft>(token, '/admin/batches', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(input)
	});

export const createEpubSectionGraphBatchDraft = (
	token: string,
	input: SectionGraphBatchDraftInput
) =>
	request<BatchDraft>(token, '/admin/section-graph-batches', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(input)
	});

export const runEpubLocalCalibration = (token: string, input: LocalCalibrationInput) =>
	request<LocalCalibrationReport>(token, '/admin/calibrations/local', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(input)
	});

export const getEpubBatchJobs = (
	token: string,
	input: { version_id?: string; offset?: number; limit?: number } = {}
) => {
	const params = new URLSearchParams();
	if (input.version_id) params.set('version_id', input.version_id);
	params.set('offset', String(input.offset ?? 0));
	params.set('limit', String(input.limit ?? 50));
	return request<EpubBatchPage>(token, `/admin/batches?${params.toString()}`);
};

export const getEpubBatchJob = (token: string, batchJobId: string) =>
	request<EpubBatchSummary>(token, `/admin/batches/${encodeURIComponent(batchJobId)}`);

/** Poll durable submitted/running jobs only; this endpoint never submits a draft. */
export const recoverEpubBatches = (token: string) =>
	request<EpubBatchRecovery>(token, '/admin/batches/recover', { method: 'POST' });

export const submitEpubBatch = (token: string, batchJobId: string) =>
	request<BatchStatus>(token, `/admin/batches/${encodeURIComponent(batchJobId)}/submit`, {
		method: 'POST'
	});

export const pollEpubBatch = (token: string, batchJobId: string) =>
	request<BatchStatus>(token, `/admin/batches/${encodeURIComponent(batchJobId)}/poll`, {
		method: 'POST'
	});

export const retryEpubBatch = (token: string, batchJobId: string) =>
	request<BatchStatus>(token, `/admin/batches/${encodeURIComponent(batchJobId)}/retry`, {
		method: 'POST'
	});

export const getEpubSampleBatchReviews = (
	token: string,
	input: { version_id?: string; job_kind?: SampleBatchReview['job_kind'] } = {}
) => {
	const params = new URLSearchParams();
	if (input.version_id) params.set('version_id', input.version_id);
	if (input.job_kind) params.set('job_kind', input.job_kind);
	return request<{ items: SampleBatchReview[] }>(token, `/admin/sample-batch-reviews?${params}`);
};

export const reviewEpubSampleBatch = (
	token: string,
	batchJobId: string,
	status: SampleBatchReview['status']
) =>
	request<SampleBatchReview>(
		token,
		`/admin/sample-batches/${encodeURIComponent(batchJobId)}/review`,
		{
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ status })
		}
	);

export const upsertEpubConcept = (token: string, input: ConceptInput) =>
	request<{ concept_id: string }>(token, '/admin/concepts', {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(input)
	});

export const getEpubRelationAssertions = (
	token: string,
	input: {
		status?: 'PROVISIONAL' | 'APPROVED' | 'REJECTED';
		version_id?: string;
		offset?: number;
		limit?: number;
	} = {}
) => {
	const params = new URLSearchParams();
	if (input.status) params.set('status', input.status);
	if (input.version_id) params.set('version_id', input.version_id);
	params.set('offset', String(input.offset ?? 0));
	params.set('limit', String(input.limit ?? 50));
	return request<RelationAssertionPage>(token, `/admin/relation-assertions?${params.toString()}`);
};

export const reviewEpubRelationAssertion = (
	token: string,
	assertionId: string,
	status: RelationAssertion['status']
) =>
	request<{ assertion_id: string; status: RelationAssertion['status'] }>(
		token,
		`/admin/relation-assertions/${encodeURIComponent(assertionId)}`,
		{
			method: 'PUT',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ status })
		}
	);

export const indexEpubRetrievalUnit = (token: string, retrievalUnitId: string) =>
	request<Record<string, unknown>>(
		token,
		`/admin/retrieval-units/${encodeURIComponent(retrievalUnitId)}/index`,
		{ method: 'POST' }
	);

export type EpubVersionIndexResult = {
	version_id: string;
	mode: 'PENDING' | 'REBUILD';
	total_retrieval_units: number;
	selected_retrieval_units: number;
	skipped_ready: number;
	ready: number;
	degraded: number;
	failed: number;
	error_count: number;
	errors: Array<{ retrieval_unit_id: string; reason: string }>;
};

export const indexEpubVersion = (token: string, versionId: string, rebuild = false) =>
	request<EpubVersionIndexResult>(token, `/admin/versions/${encodeURIComponent(versionId)}/index`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ rebuild })
	});

/** Identifiers only. Prompt text, instructions and schemas stay server-owned. */
export type EpubPromptProfiles = {
	prompt_profiles: string[];
	default_prompt_profile: string;
};

/** The server is the only authority on which prompt profiles exist. */
export const getEpubPromptProfiles = (token: string) =>
	request<EpubPromptProfiles>(token, '/admin/prompt-profiles');
