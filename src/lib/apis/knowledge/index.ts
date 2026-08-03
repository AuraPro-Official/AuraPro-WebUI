import { WEBUI_API_BASE_URL } from '$lib/constants';

export const createNewKnowledge = async (
	token: string,
	name: string,
	description: string,
	accessGrants: object[],
	meta?: Record<string, any>
) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/create`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			name: name,
			description: description,
			access_grants: accessGrants,
			meta: meta ?? null // 新增
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

export const getExternalKnowledgeConnections = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/external/connections`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
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

export const createExternalKnowledgeConnection = async (token: string, connection: object) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/external/connections`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify(connection)
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

export const updateExternalKnowledgeConnection = async (
	token: string,
	id: string,
	connection: object
) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/external/connections/${id}`, {
		method: 'PATCH',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify(connection)
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

export const deleteExternalKnowledgeConnection = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/external/connections/${id}`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
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

export const testExternalKnowledgeConnection = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/external/connections/${id}/test`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
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

export const testExternalKnowledgeRetrieval = async (
	token: string,
	id: string,
	payload: object
) => {
	let error = null;

	const res = await fetch(
		`${WEBUI_API_BASE_URL}/knowledge/external/connections/${id}/retrieve-test`,
		{
			method: 'POST',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				authorization: `Bearer ${token}`
			},
			body: JSON.stringify(payload)
		}
	)
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

export const testExternalKnowledgeSource = async (token: string, payload: object) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/external/source/test`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify(payload)
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

export const createExternalKnowledgeSource = async (token: string, payload: object) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/external/source/create`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify(payload)
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

export const updateExternalKnowledgeSource = async (token: string, id: string, payload: object) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/external/source/${id}`, {
		method: 'PATCH',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify(payload)
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

export const createExternalKnowledge = async (token: string, payload: object) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/external/knowledge/create`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify(payload)
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

export const getKnowledgeBases = async (token: string = '', page: number | null = null) => {
	let error = null;

	const searchParams = new URLSearchParams();
	if (page) searchParams.append('page', page.toString());

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/?${searchParams.toString()}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
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

export const searchKnowledgeBases = async (
	token: string = '',
	query: string | null = null,
	viewOption: string | null = null,
	page: number | null = null,
	source: string | null = null
) => {
	let error = null;

	const searchParams = new URLSearchParams();
	if (query) searchParams.append('query', query);
	if (viewOption) searchParams.append('view_option', viewOption);
	if (source) searchParams.append('source', source);
	if (page) searchParams.append('page', page.toString());

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/search?${searchParams.toString()}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
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

export const searchKnowledgeFiles = async (
	token: string,
	query?: string | null,
	viewOption?: string | null,
	orderBy?: string | null,
	direction?: string | null,
	page: number = 1,
	includeContent: boolean = false
) => {
	let error = null;

	const searchParams = new URLSearchParams();
	if (query) searchParams.append('query', query);
	if (viewOption) searchParams.append('view_option', viewOption);
	if (orderBy) searchParams.append('order_by', orderBy);
	if (direction) searchParams.append('direction', direction);
	searchParams.append('page', page.toString());
	if (includeContent) searchParams.append('include_content', 'true');

	const res = await fetch(
		`${WEBUI_API_BASE_URL}/knowledge/search/files?${searchParams.toString()}`,
		{
			method: 'GET',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				authorization: `Bearer ${token}`
			}
		}
	)
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
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

export const getKnowledgeById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
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

export const searchKnowledgeFilesById = async (
	token: string,
	id: string,
	query?: string | null,
	viewOption?: string | null,
	orderBy?: string | null,
	direction?: string | null,
	page: number = 1,
	directoryId?: string | null,
	includeContent: boolean = false
) => {
	let error = null;

	const searchParams = new URLSearchParams();
	if (query) searchParams.append('query', query);
	if (viewOption) searchParams.append('view_option', viewOption);
	if (orderBy) searchParams.append('order_by', orderBy);
	if (direction) searchParams.append('direction', direction);
	searchParams.append('page', page.toString());
	// directoryId: undefined = don't filter, null = root, string = specific dir
	if (directoryId !== undefined) {
		searchParams.append('directory_id', directoryId ?? '');
	}
	if (includeContent) searchParams.append('include_content', 'true');

	const res = await fetch(
		`${WEBUI_API_BASE_URL}/knowledge/${id}/files?${searchParams.toString()}`,
		{
			method: 'GET',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				authorization: `Bearer ${token}`
			}
		}
	)
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
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

export const getPendingKnowledgeFiles = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/files/pending`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return [];
		});

	if (error) {
		throw error;
	}

	return res;
};

export const streamPendingKnowledgeFiles = async (token: string, id: string) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/files/pending?stream=true`, {
		method: 'GET',
		headers: {
			Accept: 'text/event-stream',
			authorization: `Bearer ${token}`
		}
	});

	if (!res.ok) {
		throw new Error('Failed to stream pending files');
	}

	return res;
};

type KnowledgeUpdateForm = {
	name?: string;
	description?: string;
	data?: object;
	access_grants?: object[];
	meta?: Record<string, any>;
};

export const updateKnowledgeById = async (token: string, id: string, form: KnowledgeUpdateForm) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/update`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			name: form?.name ? form.name : undefined,
			description: form?.description ? form.description : undefined,
			data: form?.data ? form.data : undefined,
			access_grants: form.access_grants
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
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

export const updateKnowledgeAccessGrants = async (
	token: string,
	id: string,
	accessGrants: any[]
) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/access/update`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ access_grants: accessGrants })
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

export const addFileToKnowledgeById = async (
	token: string,
	id: string,
	fileId: string,
	directoryId?: string | null
) => {
	let error = null;

	const body: Record<string, string> = { file_id: fileId };
	if (directoryId) body.directory_id = directoryId;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/file/add`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify(body)
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
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

export const updateFileFromKnowledgeById = async (token: string, id: string, fileId: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/file/update`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			file_id: fileId
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
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

export const removeFileFromKnowledgeById = async (token: string, id: string, fileId: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/file/remove`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			file_id: fileId
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
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

export const resetKnowledgeById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/reset`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
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

export const syncKnowledgeDiff = async (
	token: string,
	id: string,
	manifest: Array<{ filename: string; path: string; checksum: string; size: number }>
) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/sync/diff`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ manifest })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
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

export const syncKnowledgeCleanup = async (
	token: string,
	id: string,
	fileIds: string[],
	dirIds: string[] = []
) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/sync/cleanup`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ file_ids: fileIds, dir_ids: dirIds })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
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

export const deleteKnowledgeById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/delete`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
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

export const reindexKnowledgeFiles = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/reindex`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
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

export const exportKnowledgeById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/export-with-vectors`, {
		method: 'GET',
		headers: {
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.blob();
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

// ── Directory API ───────────────────────────────────────────────────

export const importKnowledgeWithVectors = async (token: string, file: File, id: string) => {
	let error = null;

	const form = new FormData();
	form.append('file', file);
	form.append('knowledge_id', id);

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/import-with-vectors`, {
		method: 'POST',
		headers: {
			authorization: `Bearer ${token}`
		},
		body: form
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail || err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const createKnowledgeDirectory = async (
	token: string,
	id: string,
	name: string,
	parentId?: string | null
) => {
	let error = null;

	const body: Record<string, string | null> = { name };
	if (parentId) body.parent_id = parentId;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/dirs/create`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify(body)
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

export const updateKnowledgeDirectory = async (
	token: string,
	id: string,
	dirId: string,
	form: { name?: string; parent_id?: string | null }
) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/dirs/${dirId}/update`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify(form)
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

export const deleteKnowledgeDirectory = async (
	token: string,
	id: string,
	dirId: string,
	moveFiles: boolean = true
) => {
	let error = null;

	const searchParams = new URLSearchParams();
	searchParams.append('move_files', moveFiles.toString());

	const res = await fetch(
		`${WEBUI_API_BASE_URL}/knowledge/${id}/dirs/${dirId}/delete?${searchParams.toString()}`,
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
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const moveFileInKnowledge = async (
	token: string,
	id: string,
	fileId: string,
	directoryId?: string | null
) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/knowledge/${id}/file/move`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			file_id: fileId,
			directory_id: directoryId ?? null
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

export const LANG_LABELS: Record<string, string> = {
	zh: '中文',
	'zh-cn': '中文(简体)',
	'zh-tw': '中文(繁體)',
	'zh-hk': '中文(香港)',
	en: 'English',
	'en-us': 'English (US)',
	'en-gb': 'English (UK)',
	fr: 'Français',
	'fr-ca': 'Français (Canada)',
	de: 'Deutsch',
	es: 'Español',
	'es-mx': 'Español (México)',
	'es-ar': 'Español (Argentina)',
	pt: 'Português',
	'pt-br': 'Português (Brasil)',
	'pt-pt': 'Português (Portugal)',
	it: 'Italiano',
	nl: 'Nederlands',
	pl: 'Polski',
	cs: 'Čeština',
	sk: 'Slovenčina',
	hu: 'Magyar',
	ro: 'Română',
	bg: 'Български',
	hr: 'Hrvatski',
	sr: 'Српски',
	sl: 'Slovenščina',
	uk: 'Українська',
	ru: 'Русский',
	be: 'Беларуская',
	lt: 'Lietuvių',
	lv: 'Latviešu',
	et: 'Eesti',
	fi: 'Suomi',
	sv: 'Svenska',
	no: 'Norsk',
	da: 'Dansk',
	is: 'Íslenska',
	el: 'Ελληνικά',
	ar: 'العربية',
	fa: 'فارسی',
	he: 'עברית',
	tr: 'Türkçe',
	az: 'Azərbaycan',
	ka: 'ქართული',
	hy: 'Հայերեն',
	kk: 'Қазақша',
	uz: 'Oʻzbekcha',
	hi: 'हिन्दी',
	bn: 'বাংলা',
	ur: 'اردو',
	pa: 'ਪੰਜਾਬੀ',
	gu: 'ગુજરાતી',
	mr: 'मराठी',
	ta: 'தமிழ்',
	te: 'తెలుగు',
	kn: 'ಕನ್ನಡ',
	ml: 'മലയാളം',
	si: 'සිංහල',
	ne: 'नेपाली',
	ja: '日本語',
	ko: '한국어',
	vi: 'Tiếng Việt',
	th: 'ภาษาไทย',
	id: 'Bahasa Indonesia',
	ms: 'Bahasa Melayu',
	tl: 'Filipino',
	km: 'ភាសាខ្មែរ',
	lo: 'ລາວ',
	my: 'မြန်မာဘာသာ',
	mn: 'Монгол',
	sw: 'Kiswahili',
	am: 'አማርኛ',
	yo: 'Yorùbá',
	ig: 'Igbo',
	ha: 'Hausa',
	zu: 'isiZulu',
	af: 'Afrikaans',
	eu: 'Euskara',
	ca: 'Català',
	gl: 'Galego',
	cy: 'Cymraeg',
	ga: 'Gaeilge',
	mt: 'Malti',
	sq: 'Shqip',
	mk: 'Македонски',
	bs: 'Bosanski'
};

const LANG_PATTERN = /^([a-z]{2}(?:-[a-z]{2})?)[-_.](.+)$|^(.+?)[-_.]([a-z]{2}(?:-[a-z]{2})?)$/i;

export function parseFileName(name: string): { base: string; lang: string } | null {
	const nameWithoutExt = name.replace(/\.[^.]+$/, '');
	const match = nameWithoutExt.match(LANG_PATTERN);
	if (!match) return null;
	if (match[1] && match[2]) return { lang: match[1].toLowerCase(), base: match[2] };
	if (match[3] && match[4]) return { lang: match[4].toLowerCase(), base: match[3] };
	return null;
}

export type SentenceAlignItem = {
	id: string;
	align_group_id: string;
	para_index: number;
	sentence_index: number;
	primary_text: string;
	start: number;
	end: number;
	langs: Record<string, string>;
	langs_modified: Record<string, boolean>;
	align_score: number;
};

export type ParagraphAlignItem = {
	para_index: number;
	para_text: string;
	sentences: SentenceAlignItem[];
};

export type GetBilingualAlignResponse = {
	bilingual_id: string;
	primary_lang: string;
	languages: string[];
	paragraphs: ParagraphAlignItem[];
};

export const getBilingualAlign = async (
	token: string,
	bilingualId: string,
	collectionName: string
): Promise<GetBilingualAlignResponse> => {
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/retrieval/process/bilingual/${bilingualId}?collection_name=${encodeURIComponent(collectionName)}`,
		{
			headers: {
				Authorization: `Bearer ${token}`
			}
		}
	);
	if (!res.ok) throw new Error(await res.text());
	return res.json();
};

export const updateSentenceTranslation = async (
	token: string,
	payload: { collection_name: string; align_group_id: string; lang: string; text: string }
): Promise<{
	status: boolean;
	align_group_id: string;
	lang: string;
	langs_modified: boolean;
}> => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/retrieval/process/bilingual/sentence`, {
		method: 'PUT',
		headers: {
			Authorization: `Bearer ${token}`,
			'Content-Type': 'application/json'
		},
		body: JSON.stringify(payload)
	});
	if (!res.ok) throw new Error(await res.text());
	return res.json();
};

export type GlossaryTerm = {
	id: string;
	lang: string;
	source: string;
	target: string;
};

export type GetBilingualWordsResponse = {
	bilingual_id: string;
	primary_lang: string;
	languages: string[];
	terms: Record<string, GlossaryTerm[]>; // lang -> 术语条目
};

export const getBilingualWords = async (
	token: string,
	bilingualId: string,
	collectionName: string
): Promise<GetBilingualWordsResponse> => {
	let error = null;

	const searchParams = new URLSearchParams({ collection_name: collectionName });

	const res = await fetch(
		`${WEBUI_API_BASE_URL}/retrieval/bilingual/${bilingualId}/words?${searchParams.toString()}`,
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
			error = err?.detail ?? '获取术语表失败';
			return null;
		});

	if (error) throw new Error(error);
	return res;
};

export const updateBilingualWords = async (
	token: string,
	payload: {
		collection_name: string;
		bilingual_id: string;
		lang: string;
		terms: { source: string; target: string }[];
	}
): Promise<{ status: boolean; updated_chunks: number; term_count: number }> => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/retrieval/bilingual/words`, {
		method: 'PUT',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify(payload)
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err?.detail ?? '保存术语表失败';
			return null;
		});

	if (error) throw new Error(error);
	return res;
};
