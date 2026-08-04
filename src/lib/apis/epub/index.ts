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
	degraded: EpubDegradedState[];
};

export type BatchDraftInput = {
	version_id: string;
	profile_name: string;
	is_sample: boolean;
	sample_limit: number;
};

export type BatchDraft = {
	batch_job_id: string;
	item_count: number;
	status: string;
};

export type BatchStatus = Record<string, string | number | null | undefined>;

export type ConceptInput = {
	canonical_name: string;
	aliases: string[];
	definition: string;
	status: 'PROVISIONAL' | 'APPROVED' | 'REJECTED';
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

export const upsertEpubConcept = (token: string, input: ConceptInput) =>
	request<{ concept_id: string }>(token, '/admin/concepts', {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(input)
	});

export const indexEpubRetrievalUnit = (token: string, retrievalUnitId: string) =>
	request<Record<string, unknown>>(
		token,
		`/admin/retrieval-units/${encodeURIComponent(retrievalUnitId)}/index`,
		{ method: 'POST' }
	);
