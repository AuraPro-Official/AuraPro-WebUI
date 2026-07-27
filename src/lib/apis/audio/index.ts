import { AUDIO_API_BASE_URL } from '$lib/constants';

const getErrorMessage = (err: any) => {
	const detail = err?.detail ?? err?.message ?? err?.error ?? err;

	if (Array.isArray(detail)) {
		return detail
			.map((item) => {
				if (typeof item === 'string') {
					return item;
				}

				if (item?.msg) {
					const location = Array.isArray(item.loc) ? item.loc.join('.') : item.loc;
					return location ? `${location}: ${item.msg}` : item.msg;
				}

				return JSON.stringify(item);
			})
			.join('\n');
	}

	if (typeof detail === 'object' && detail !== null) {
		return JSON.stringify(detail);
	}

	return detail ? `${detail}` : 'Request failed';
};

const parseResponseBody = async (res: Response) => {
	const text = await res.text();
	if (!text) {
		return null;
	}

	try {
		return JSON.parse(text);
	} catch {
		return text;
	}
};

const parseJsonResponse = async (res: Response) => {
	const body = await parseResponseBody(res);
	if (!res.ok) {
		throw body ?? res.statusText;
	}
	return body;
};

const throwIfErrorResponse = async (res: Response) => {
	if (!res.ok) {
		throw (await parseResponseBody(res)) ?? res.statusText;
	}
	return res;
};

export const getAudioConfig = async (token: string) => {
	let error = null;

	const res = await fetch(`${AUDIO_API_BASE_URL}/config`, {
		method: 'GET',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			return parseJsonResponse(res);
		})
		.catch((err) => {
			console.error(err);
			error = getErrorMessage(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const updateAudioConfig = async (token: string, payload: Record<string, any>) => {
	let error = null;

	const res = await fetch(`${AUDIO_API_BASE_URL}/config/update`, {
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
			return parseJsonResponse(res);
		})
		.catch((err) => {
			console.error(err);
			error = getErrorMessage(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const transcribeAudio = async (
	token: string,
	file: File,
	language?: string,
	options?: {
		streamId?: string;
		chunkIndex?: number;
		resetStream?: boolean;
		languageCandidates?: string[] | string;
	}
) => {
	const data = new FormData();
	data.append('file', file);
	if (language) {
		data.append('language', language);
	}
	if (options?.languageCandidates) {
		data.append(
			'language_candidates',
			Array.isArray(options.languageCandidates)
				? options.languageCandidates.filter(Boolean).join(',')
				: options.languageCandidates
		);
	}
	if (options?.streamId) {
		data.append('stream_id', options.streamId);
	}
	if (options?.chunkIndex !== undefined) {
		data.append('chunk_index', String(options.chunkIndex));
	}
	if (options?.resetStream !== undefined) {
		data.append('reset_stream', String(options.resetStream));
	}

	let error = null;
	const res = await fetch(`${AUDIO_API_BASE_URL}/transcriptions`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`
		},
		body: data
	})
		.then(async (res) => {
			return parseJsonResponse(res);
		})
		.catch((err) => {
			error = getErrorMessage(err);
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const synthesizeOpenAISpeech = async (
	token: string = '',
	speaker: string = 'alloy',
	text: string = '',
	model?: string
) => {
	let error = null;

	const res = await fetch(`${AUDIO_API_BASE_URL}/speech`, {
		method: 'POST',
		headers: {
			Authorization: `Bearer ${token}`,
			'Content-Type': 'application/json'
		},
		body: JSON.stringify({
			input: text,
			voice: speaker,
			...(model && { model })
		})
	})
		.then(async (res) => {
			return throwIfErrorResponse(res);
		})
		.catch((err) => {
			error = getErrorMessage(err);
			console.error(err);

			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

interface AvailableModelsResponse {
	models: { name: string; id: string }[] | { id: string }[];
}

export const getModels = async (token: string = ''): Promise<AvailableModelsResponse> => {
	let error = null;

	const res = await fetch(`${AUDIO_API_BASE_URL}/models`, {
		method: 'GET',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			return parseJsonResponse(res);
		})
		.catch((err) => {
			error = getErrorMessage(err);
			console.error(err);

			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getVoices = async (token: string = '') => {
	let error = null;

	const res = await fetch(`${AUDIO_API_BASE_URL}/voices`, {
		method: 'GET',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			return parseJsonResponse(res);
		})
		.catch((err) => {
			error = getErrorMessage(err);
			console.error(err);

			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};
