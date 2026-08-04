import { RETRIEVAL_API_BASE_URL } from '$lib/constants';

export const getRAGConfig = async (token: string) => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/config`, {
		method: 'GET',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

type ChunkConfigForm = {
	chunk_size: number;
	chunk_overlap: number;
};

type DocumentIntelligenceConfigForm = {
	key: string;
	endpoint: string;
	model: string;
};

type ContentExtractConfigForm = {
	engine: string;
	tika_server_url: string | null;
	document_intelligence_config: DocumentIntelligenceConfigForm | null;
};

type YoutubeConfigForm = {
	language: string[];
	translation?: string | null;
	proxy_url: string;
};

type RAGConfigForm = {
	PDF_EXTRACT_IMAGES?: boolean;
	ENABLE_GOOGLE_DRIVE_INTEGRATION?: boolean;
	ENABLE_ONEDRIVE_INTEGRATION?: boolean;
	EXTERNAL_DOCUMENT_LOADER_HEADERS?: Record<string, string>;
	chunk?: ChunkConfigForm;
	content_extraction?: ContentExtractConfigForm;
	web_loader_ssl_verification?: boolean;
	web?: Record<string, unknown>;
	youtube?: YoutubeConfigForm;
};

export const updateRAGConfig = async (token: string, payload: RAGConfigForm) => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/config/update`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			...payload
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getQuerySettings = async (token: string) => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/query/settings`, {
		method: 'GET',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

type QuerySettings = {
	k: number | null;
	r: number | null;
	template: string | null;
};

export const updateQuerySettings = async (token: string, settings: QuerySettings) => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/query/settings/update`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			...settings
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getEmbeddingConfig = async (token: string) => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/embedding`, {
		method: 'GET',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

type OpenAIConfigForm = {
	key: string;
	url: string;
};

type AzureOpenAIConfigForm = {
	key: string;
	url: string;
	version: string;
};

type EmbeddingModelUpdateForm = {
	openai_config?: OpenAIConfigForm;
	azure_openai_config?: AzureOpenAIConfigForm;
	embedding_engine: string;
	embedding_model: string;
	embedding_batch_size?: number;
};

export const updateEmbeddingConfig = async (token: string, payload: EmbeddingModelUpdateForm) => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/embedding/update`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			...payload
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getRerankingConfig = async (token: string) => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/reranking`, {
		method: 'GET',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

type RerankingModelUpdateForm = {
	reranking_model: string;
};

export const updateRerankingConfig = async (token: string, payload: RerankingModelUpdateForm) => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/reranking/update`, {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			...payload
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const processYoutubeVideo = async (token: string, url: string) => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/process/youtube`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			url: url
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const processWeb = async (
	token: string,
	collection_name: string,
	url: string,
	process: boolean = true
) => {
	let error = null;

	const searchParams = new URLSearchParams();

	if (!process) {
		searchParams.append('process', 'false');
	}

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/process/web?${searchParams.toString()}`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			url: url,
			collection_name: collection_name
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const queryDoc = async (
	token: string,
	collection_name: string,
	query: string,
	k: number | null = null
) => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/query/doc`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			collection_name: collection_name,
			query: query,
			k: k
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const queryCollection = async (
	token: string,
	collection_names: string,
	query: string,
	k: number | null = null
) => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/query/collection`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			collection_names: collection_names,
			query: query,
			k: k
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const resetUploadDir = async (token: string) => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/reset/uploads`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const resetVectorDB = async (token: string) => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/reset/db`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export interface BilingualEpubFile {
	id: string;
	baseName: string;
	langs: Record<LangCode, File>;
	primaryLang: LangCode;
}

export interface KnowledgeImportProgress {
	stage: string;
	progress: number;
	current?: number;
	total?: number;
	cold_start?: boolean;
}

export type KnowledgeImportProgressHandler = (progress: KnowledgeImportProgress) => void;

export const readKnowledgeImportProgress = async (
	response: Response,
	onProgress: KnowledgeImportProgressHandler
) => {
	const reader = response.body?.getReader();
	if (!reader) {
		throw new Error('Knowledge import response is not readable');
	}

	const decoder = new TextDecoder();
	let buffer = '';
	let result: unknown = null;

	const consumeLine = (line: string) => {
		const trimmed = line.trim();
		if (!trimmed) return;

		const event = JSON.parse(trimmed);
		if (event.type === 'progress') {
			onProgress(event as KnowledgeImportProgress);
		} else if (event.type === 'result') {
			result = event.result;
		} else if (event.type === 'error') {
			throw new Error(event.error || 'Knowledge import failed');
		}
	};

	while (true) {
		const { done, value } = await reader.read();
		if (done) break;

		buffer += decoder.decode(value, { stream: true });
		const lines = buffer.split('\n');
		buffer = lines.pop() ?? '';
		for (const line of lines) consumeLine(line);
	}

	buffer += decoder.decode();
	if (buffer.trim()) consumeLine(buffer);
	if (result === null) {
		throw new Error('Knowledge import ended without a result');
	}
	return result;
};

/**
 * 处理EPUB文件 - 上传并分割为章节
 *
 * 流程：
 * 1. 前端选择EPUB文件
 * 2. 上传到后端 /api/process/bilingual/epub
 * 3. 后端分割并返回章节内容
 * 4. 前端显示预览
 * 5. 用户确认后进行最终导入
 */
export const processEpubFile = async (
	token: string,
	collection_name: string,
	file: BilingualEpubFile,
	onProgress?: KnowledgeImportProgressHandler
) => {
	const formData = new FormData();
	formData.append('collection_name', collection_name);
	formData.append('primaryLang', file.primaryLang);

	for (const [lang, langFile] of Object.entries(file.langs)) {
		formData.append('files', langFile);
		formData.append('langs', lang);
	}

	const response = await fetch(
		`${RETRIEVAL_API_BASE_URL}/process/bilingual/epub${onProgress ? '?stream=true' : ''}`,
		{
			method: 'POST',
			headers: {
				Accept: onProgress ? 'application/x-ndjson' : 'application/json',
				Authorization: `Bearer ${token}`
			},
			body: formData
		}
	);

	if (!response.ok) {
		const err = await response.json().catch(() => ({}));
		throw new Error(err?.detail ?? `HTTP ${response.status}`);
	}
	return onProgress
		? await readKnowledgeImportProgress(response, onProgress)
		: await response.json();
};

export type LangCode = string;

export interface BilingualFile {
	id: string;
	baseName: string;
	langs: Record<LangCode, string>;
	primaryLang: LangCode;
	primaryText: string;
}

export interface BilingualImportData {
	files: BilingualFile[] | BilingualEpubFile[];
	languages: LangCode[];
	primaryLang: LangCode;
	totalFiles: number;
}

export const processBilingual = async (
	token: string,
	collectionName: string | null,
	files: BilingualFile[],
	languages: string[],
	primaryLang: string,
	onProgress?: KnowledgeImportProgressHandler
) => {
	const response = await fetch(
		`${RETRIEVAL_API_BASE_URL}/process/bilingual${onProgress ? '?stream=true' : ''}`,
		{
			method: 'POST',
			headers: {
				Accept: onProgress ? 'application/x-ndjson' : 'application/json',
				'Content-Type': 'application/json',
				Authorization: `Bearer ${token}`
			},
			body: JSON.stringify({
				collection_name: collectionName ?? null,
				files,
				languages,
				primaryLang,
				totalFiles: files.length
			})
		}
	);

	if (!response.ok) {
		const err = await response.json().catch(() => ({}));
		throw new Error(err?.detail ?? `HTTP ${response.status}`);
	}

	return onProgress
		? await readKnowledgeImportProgress(response, onProgress)
		: await response.json();
};

export const getBilingualFiles = async (
	token: string,
	collection_name: string,
	skip: number = 0,
	limit: number = 50
) => {
	let error = null;

	const res = await fetch(
		`${RETRIEVAL_API_BASE_URL}/process/bilingual/files?collection_name=${encodeURIComponent(
			collection_name
		)}&skip=${skip}&limit=${limit}`,
		{
			method: 'GET',
			headers: {
				Accept: 'application/json',
				authorization: `Bearer ${token}`
			}
		}
	)
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err?.detail ?? err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}
	return res;
};

export const deleteBilingualFile = async (
	token: string,
	collection_name: string,
	bilingual_id: string
) => {
	let error = null;

	const res = await fetch(
		`${RETRIEVAL_API_BASE_URL}/process/bilingual/file?collection_name=${encodeURIComponent(
			collection_name
		)}&bilingual_id=${encodeURIComponent(bilingual_id)}`,
		{
			method: 'DELETE',
			headers: {
				Accept: 'application/json',
				authorization: `Bearer ${token}`
			}
		}
	)
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err?.detail ?? err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}
	return res;
};

export type GoogleSheetPreview = {
	languagePairs: string[];
	languages: string[];
	rowCount: number;
	rows: Record<string, string>[];
};

export type GoogleSheetImportResult = {
	files: {
		id: string;
		baseName: string;
		languages: string[];
		paragraphs: { index: number; langs: Record<string, string> }[];
		primaryLang: string;
		primaryText: string;
		docLinks: Record<string, string>;
		errors: Record<string, string>;
	}[];
	primaryLang: string;
	languages: string[];
	totalFiles: number;
	rowErrors: { row: number; baseName: string; reason: string }[];
};

export const previewBilingualGoogleSheet = async (
	token: string,
	sheetUrl: string
): Promise<GoogleSheetPreview> => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/process/bilingual/google-sheet/preview`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ sheet_url: sheetUrl })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err?.detail ?? '解析表格失败';
			return null;
		});

	if (error) throw new Error(error);
	return res;
};

export const importBilingualGoogleSheet = async (
	token: string,
	sheetUrl: string,
	primaryLang?: string,
	skipFirstTableRow: boolean = false
): Promise<GoogleSheetImportResult> => {
	let error = null;

	const res = await fetch(`${RETRIEVAL_API_BASE_URL}/process/bilingual/google-sheet`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			sheet_url: sheetUrl,
			primary_lang: primaryLang ?? null,
			skip_first_table_row: skipFirstTableRow
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err?.detail ?? '导入 Google 表格失败';
			return null;
		});

	if (error) throw new Error(error);
	return res;
};
