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
	/**
	 * Relations this item ingested past because both endpoints had already been
	 * merged into one concept. A count only — the column is an integer or NULL,
	 * so it cannot carry a concept name or source text. `null` means nothing was
	 * measured: a `CONCEPT_MENTIONS` item, an item that has not succeeded, or a
	 * row written before the column existed.
	 */
	skipped_self_relations: number | null;
	/**
	 * Evidence spans this item dropped for being below the floor its prompt
	 * profile enforces — concept mentions and relation evidence share one
	 * counter, since they are the same defect with the same fix. A count only,
	 * for the same schema reason as above. The dropped spans are absent from the
	 * stored response, so this is the only record that they existed. `null` means
	 * nothing was measured: an item whose payload never went through a grounding
	 * pass, or a row written before the column existed.
	 */
	skipped_short_evidence: number | null;
	/**
	 * Concepts this item skipped because their name and aliases matched more than
	 * one existing concept — linking one would assert a merge no administrator
	 * decided. The concept's mentions are skipped with it, and a relation naming
	 * it is dropped. A count only, for the same schema reason as above. Unlike
	 * `skipped_short_evidence`, the skipped concept is still in the stored
	 * response verbatim: it is discovered at write time, not by the read-only
	 * grounding pass, so this column records what the write did rather than what
	 * the response omits. `null` only means the row was written before the column
	 * existed — every succeeded item resolves concepts, so a `0` is a real zero on
	 * both job kinds.
	 */
	skipped_ambiguous_concepts: number | null;
	/**
	 * Evidence spans this item dropped because they could not be verified
	 * against the passage they named — the model quoted text that is not there,
	 * or text that is there more than once with nothing to select an occurrence.
	 * The claim goes with the citation: a concept left with no mention is
	 * dropped, and a relation left with no evidence, or whose endpoint was one
	 * of those concepts, goes with it. A count only, for the same schema reason
	 * as above. Deliberately not folded into `skipped_short_evidence`: a
	 * sub-floor span is our own threshold, this is the model's bookkeeping, and
	 * one must not mask the other. `null` means the rule could not run: a
	 * `CONCEPT_MENTIONS` item, whose ungrounded spans still fail it whole, an
	 * item that has not succeeded, or a row written before the column existed.
	 */
	skipped_ungrounded_evidence: number | null;
	updated_at: string | null;
};

/** Lifecycle-only operational history. Prompts, model output and raw errors are excluded. */
export type EpubBatchSummary = {
	batch_job_id: string;
	version_id: string;
	provider: string;
	provider_job_id: string | null;
	profile_name: string;
	/**
	 * Extraction instruction identifier, never its text. `null` means the job
	 * predates the column: the full-run approval gate binds to this value, so an
	 * unknown one unlocks nothing until the backfill derives it.
	 */
	prompt_profile: string | null;
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
	/**
	 * Merged-away self-relations skipped across this job's succeeded items. A
	 * succeeded item is the one an administrator never opens, so the count is
	 * aggregated here rather than only inside each item.
	 */
	item_skipped_self_relations: number;
	/**
	 * Sub-floor evidence spans dropped across this job's succeeded items,
	 * aggregated here for the same reason as the count above.
	 */
	item_skipped_short_evidence: number;
	/**
	 * Ambiguous concepts skipped across this job's succeeded items, aggregated
	 * here for the same reason as the counts above. Counts concepts, not the
	 * relations that cascaded off them.
	 */
	item_skipped_ambiguous_concepts: number;
	/**
	 * Unverifiable evidence spans dropped across this job's succeeded items,
	 * aggregated here for the same reason as the counts above. Kept separate
	 * from `item_skipped_short_evidence` because the two settle differently.
	 */
	item_skipped_ungrounded_evidence: number;
	items?: EpubBatchItemSummary[];
};

export type EpubBatchPage = { total: number; offset: number; items: EpubBatchSummary[] };
export type EpubBatchRecovery = {
	recovered: Array<{ job_id: string; state: string; ingested: number; failed: number }>;
	skipped: Array<{ job_id: string; provider: string; reason: string }>;
};

/**
 * Per-job outcome of recovering the prompt profile of jobs created before it
 * was recorded. Identifiers and content-free reason classes only: an
 * unresolved job keeps no profile and therefore keeps failing the gate.
 */
export type EpubBatchPromptProfileBackfill = {
	examined: number;
	resolved: Array<{
		batch_job_id: string;
		job_kind: 'CONCEPT_MENTIONS' | 'SECTION_GRAPH';
		prompt_profile: string;
	}>;
	unresolved: Array<{
		batch_job_id: string;
		job_kind: string;
		reason: string;
	}>;
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

/** Concept labels and counts only. No passage text, evidence or model output. */
export type EpubConcept = {
	concept_id: string;
	canonical_name: string;
	definition: string;
	status: 'PROVISIONAL' | 'APPROVED' | 'REJECTED';
	aliases: string[];
	mention_count: number;
	created_at?: string | null;
	updated_at?: string | null;
};

export type EpubConceptPage = { total: number; offset: number; items: EpubConcept[] };

/**
 * Fold `source_concept_id` into `target_concept_id`. Ingest refuses an item
 * whose model suggestion matches two concepts exactly; this is the only
 * administrator action that resolves such a candidate.
 */
export type ConceptMergeInput = {
	target_concept_id: string;
	source_concept_id: string;
	/** Optional preferred spelling for the surviving concept. */
	canonical_name?: string;
};

export type ConceptMergeResult = {
	concept_merge_id: string;
	target_concept_id: string;
	source_concept_id: string;
	source_canonical_name: string;
	canonical_name: string;
	status: EpubConcept['status'];
	merged_by: string;
	merged_at: string;
	moved_aliases: number;
	moved_mentions: number;
	duplicate_mentions: number;
	repointed_relations: number;
	folded_relations: number;
	/** Relations between the two merged concepts; a self-loop cannot survive. */
	dropped_self_relations: number;
};

/**
 * One mention, named the way the API names a mention everywhere else: by its
 * passage and code-point span. Omit both offsets for an unanchored mention.
 */
export type ConceptMentionRef = {
	passage_id: string;
	start_codepoint?: number | null;
	end_codepoint?: number | null;
};

/**
 * Carve part of `source_concept_id` out into a new concept. A merge is
 * one-way, and this is the only administrator action that corrects one — as an
 * explicit new decision, not a rewind: the caller states which aliases and
 * which mentions become the new concept. `canonical_name` must be one of the
 * moving aliases or a spelling no concept already owns.
 */
export type ConceptSplitInput = {
	source_concept_id: string;
	canonical_name: string;
	aliases: string[];
	mentions: ConceptMentionRef[];
};

export type ConceptSplitResult = {
	concept_split_id: string;
	source_concept_id: string;
	source_canonical_name: string;
	new_concept_id: string;
	canonical_name: string;
	status: EpubConcept['status'];
	split_by: string;
	split_at: string;
	moved_aliases: number;
	moved_mentions: number;
	/** Relations left on the source; a split never repoints one automatically. */
	relations_on_source: number;
	/** Of those, the ones whose evidence names a split-off spelling — review by hand. */
	relations_naming_split_aliases: number;
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
	/** Which extraction instruction this approval covers; `null` if unrecorded. */
	prompt_profile: string | null;
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

/**
 * Derive the prompt profile of jobs that predate the column, from the
 * instruction each job actually sent. Never guesses: a job that matches no
 * registered profile exactly is reported unresolved and keeps failing the gate.
 */
export const backfillEpubBatchPromptProfiles = (token: string) =>
	request<EpubBatchPromptProfileBackfill>(token, '/admin/batches/backfill-prompt-profiles', {
		method: 'POST'
	});

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

export const getEpubConcepts = (
	token: string,
	input: {
		status?: EpubConcept['status'];
		offset?: number;
		limit?: number;
	} = {}
) => {
	const params = new URLSearchParams();
	if (input.status) params.set('status', input.status);
	params.set('offset', String(input.offset ?? 0));
	params.set('limit', String(input.limit ?? 50));
	return request<EpubConceptPage>(token, `/admin/concepts?${params.toString()}`);
};

export const mergeEpubConcepts = (token: string, input: ConceptMergeInput) =>
	request<ConceptMergeResult>(token, '/admin/concepts/merge', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(input)
	});

export const splitEpubConcept = (token: string, input: ConceptSplitInput) =>
	request<ConceptSplitResult>(token, '/admin/concepts/split', {
		method: 'POST',
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

/**
 * A published analysis overlay. Concept labels, aliases and definitions are
 * the analysis product and travel; passage text never does. Mentions and
 * relation evidence are locations only — `(ordinal, content_sha256)` plus
 * code-point offsets — which the receiving server verifies against its own
 * copy of the book before deriving each evidence string from it.
 */
export type EpubOverlayLocation = {
	ordinal: number;
	content_sha256: string;
	start_codepoint: number;
	end_codepoint: number;
};

export type EpubOverlayConcept = {
	/** Normalized canonical name; the join key for mentions and relations. */
	key: string;
	canonical_name: string;
	aliases: string[];
	definition: string;
	status: EpubConcept['status'];
};

export type EpubOverlayMention = EpubOverlayLocation & { concept_key: string };

export type EpubOverlayRelation = {
	subject_key: string;
	predicate: string;
	object_key: string;
	status: EpubConcept['status'];
	evidence: EpubOverlayLocation[];
};

export type EpubConceptOverlay = {
	overlay_format_version: number;
	epub_sha256: string;
	parser_version: string;
	book_title: string;
	/** Digest of the whole ordered passage set the analysis was built against. */
	passage_fingerprint: { count: number; digest: string };
	concepts: EpubOverlayConcept[];
	mentions: EpubOverlayMention[];
	relations: EpubOverlayRelation[];
};

export type EpubOverlayDownload = {
	overlay: EpubConceptOverlay;
	/** SHA-256 of the exact bytes below, for publishing alongside the file. */
	overlay_sha256: string;
	/** Canonical artifact bytes; re-serializing the object would not match. */
	text: string;
};

/** Counts and stable reason classes only; never passage text or labels. */
export type EpubOverlayApplyResult = {
	version_id: string;
	epub_sha256: string;
	overlay_format_version: number;
	applied: number;
	skipped: number;
	rejected: number;
	applied_detail: Record<string, number>;
	skipped_detail: Record<string, number>;
	skipped_reasons: Record<string, number>;
	rejection_reasons: Record<string, number>;
	book_id?: string | null;
	book_title?: string | null;
	uploaded_overlay_sha256: string;
	canonical_overlay_sha256: string;
	/** An imported overlay carries no vectors; rebuild the version index. */
	vectors_require_reindex: boolean;
};

/**
 * Download one version's analysis as publishable artifact bytes. The exact
 * body is kept because the server's `X-Overlay-SHA256` covers those bytes;
 * `JSON.stringify` of the parsed object would produce a different digest.
 */
export const getEpubVersionOverlay = async (
	token: string,
	versionId: string
): Promise<EpubOverlayDownload> => {
	const response = await fetch(
		epubUrl(`/admin/versions/${encodeURIComponent(versionId)}/overlay`),
		{
			headers: { Accept: 'application/json', authorization: `Bearer ${token}` }
		}
	);
	const text = await response.text();
	if (!response.ok) {
		let body: unknown = text;
		try {
			body = JSON.parse(text);
		} catch {
			/* a non-JSON error body is reported verbatim */
		}
		throw new Error(errorDetail(body, response.statusText));
	}
	return {
		overlay: JSON.parse(text) as EpubConceptOverlay,
		overlay_sha256: response.headers.get('X-Overlay-SHA256') ?? '',
		text
	};
};

/** Apply a published overlay to this server's own copy of the same EPUB. */
export const applyEpubOverlay = (token: string, file: File) => {
	const data = new FormData();
	data.append('file', file);
	return request<EpubOverlayApplyResult>(token, '/admin/overlays', {
		method: 'POST',
		body: data
	});
};

/** Identifiers only. Prompt text, instructions and schemas stay server-owned. */
export type EpubPromptProfiles = {
	prompt_profiles: string[];
	default_prompt_profile: string;
};

/** The server is the only authority on which prompt profiles exist. */
export const getEpubPromptProfiles = (token: string) =>
	request<EpubPromptProfiles>(token, '/admin/prompt-profiles');
