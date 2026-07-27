import { WEBUI_API_BASE_URL } from '$lib/constants';

export const getGlossarySettings = async (token: string) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/glossary/settings`, {
		method: 'GET',
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
			console.error(err);
			throw err?.detail ?? err;
		});

	return res?.settings ?? res;
};

export const getGlossary = async (token: string) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/glossary/`, {
		method: 'GET',
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
			console.error(err);
			throw err?.detail ?? err;
		});

	return res;
};

export const updateGlossarySettings = async (token: string, settings: Record<string, unknown>) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/glossary/settings`, {
		method: 'PUT',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify(settings)
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			throw err?.detail ?? err;
		});

	return res?.settings ?? res;
};

export const createGlossary = async (
	token: string,
	name = '',
	source_lang?: string,
	glossary_lang?: string,
	target_lang?: string
) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/glossary/glossaries`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ name, source_lang, glossary_lang, target_lang })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			throw err?.detail ?? err;
		});

	return res?.settings ?? res;
};

export const activateGlossary = async (token: string, id: string) => {
	const res = await fetch(
		`${WEBUI_API_BASE_URL}/glossary/glossaries/${encodeURIComponent(id)}/activate`,
		{
			method: 'POST',
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
			console.error(err);
			throw err?.detail ?? err;
		});

	return res?.settings ?? res;
};

export const deleteGlossary = async (token: string, id: string) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/glossary/glossaries/${encodeURIComponent(id)}`, {
		method: 'DELETE',
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
			console.error(err);
			throw err?.detail ?? err;
		});

	return res?.settings ?? res;
};

export const exportGlossary = async (token: string) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/glossary/export`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.blob();
		})
		.catch((err) => {
			console.error(err);
			throw err?.detail ?? err;
		});

	return res;
};

export const upsertGlossaryEntry = async (
	token: string,
	source: string,
	target: string,
	metadata: { chat_id?: string; message_id?: string; note?: string } = {}
) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/glossary/entry`, {
		method: 'PUT',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			source,
			target,
			...metadata
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			throw err?.detail ?? err;
		});

	return res;
};

export const deleteGlossaryEntry = async (token: string, source: string) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/glossary/entry/${encodeURIComponent(source)}`, {
		method: 'DELETE',
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
			console.error(err);
			throw err?.detail ?? err;
		});

	return res;
};

export const deleteGlossaryEntries = async (token: string, sources: string[]) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/glossary/entries/delete`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ sources })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			throw err?.detail ?? err;
		});

	return res;
};

export const importGlossaryEntries = async (
	token: string,
	content: string,
	replace: boolean = false,
	as_new_glossary: boolean = false
) => {
	const res = await fetch(`${WEBUI_API_BASE_URL}/glossary/import`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ content, replace, as_new_glossary })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			throw err?.detail ?? err;
		});

	return res;
};
