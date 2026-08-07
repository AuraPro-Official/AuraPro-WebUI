/**
 * Shared helpers for managing system-level connections.
 * Used by both the admin settings UI and the desktop event handler
 * to ensure consistent add/remove logic.
 */

import { getOpenAIConfig, updateOpenAIConfig } from '$lib/apis/openai';
import { getTerminalServerConnections, setTerminalServerConnections } from '$lib/apis/configs';

const DEFAULT_AURAPRO_LLAMA_URL = 'http://127.0.0.1:18881/v1';

const normalizeConnectionUrl = (url: string): string => url.replace(/\/$/, '');

const isAuraProLocalLlamaUrl = (url: unknown): boolean => {
	if (typeof url !== 'string') return false;

	try {
		const parsed = new URL(url);
		const hostname = parsed.hostname.toLowerCase();
		const port = Number(parsed.port);
		const pathname = parsed.pathname.replace(/\/$/, '');

		return (
			(hostname === '127.0.0.1' || hostname === 'localhost') &&
			port >= 18881 &&
			port <= 18981 &&
			pathname === '/v1'
		);
	} catch {
		return false;
	}
};

export const normalizeAuraProLocalLlamaConnections = async (
	token: string,
	targetUrl = DEFAULT_AURAPRO_LLAMA_URL
) => {
	const current = await getOpenAIConfig(token);
	const urls: string[] = current?.OPENAI_API_BASE_URLS ?? [];
	const keys: string[] = current?.OPENAI_API_KEYS ?? [];
	const configs: Record<string, Record<string, unknown> | undefined> =
		current?.OPENAI_API_CONFIGS ?? {};
	const localIndexes = urls
		.map((url: string, idx: number) => (isAuraProLocalLlamaUrl(url) ? idx : -1))
		.filter((idx: number) => idx >= 0);

	if (localIndexes.length === 0) return current;

	const normalizedTarget = normalizeConnectionUrl(targetUrl);
	const firstLocalIndex = localIndexes[0];
	const firstConfig = configs[firstLocalIndex.toString()] ?? configs[urls[firstLocalIndex]] ?? {};
	const alreadyNormalized =
		localIndexes.length === 1 &&
		normalizeConnectionUrl(urls[firstLocalIndex]) === normalizedTarget &&
		firstConfig?.connection_type === 'local' &&
		firstConfig?.provider === 'llama.cpp' &&
		keys.length === urls.length;

	if (alreadyNormalized) return current;

	const localIndexSet = new Set(localIndexes);
	const newUrls: string[] = [];
	const newKeys: string[] = [];
	const newConfigs: Record<string, Record<string, unknown> | undefined> = {};
	let localWritten = false;

	urls.forEach((url: string, idx: number) => {
		const isLocalLlama = localIndexSet.has(idx);
		if (isLocalLlama && localWritten) return;

		const newIdx = newUrls.length;
		const existingConfig = configs[idx.toString()] ?? configs[url];

		if (isLocalLlama) {
			newUrls.push(normalizedTarget);
			newKeys.push(keys[idx] ?? '');
			newConfigs[newIdx.toString()] = {
				...(existingConfig ?? {}),
				connection_type: 'local',
				provider: 'llama.cpp'
			};
			localWritten = true;
			return;
		}

		newUrls.push(normalizeConnectionUrl(url));
		newKeys.push(keys[idx] ?? '');
		if (existingConfig !== undefined) {
			newConfigs[newIdx.toString()] = existingConfig;
		}
	});

	return await updateOpenAIConfig(token, {
		ENABLE_OPENAI_API: current?.ENABLE_OPENAI_API ?? true,
		OPENAI_API_BASE_URLS: newUrls,
		OPENAI_API_KEYS: newKeys,
		OPENAI_API_CONFIGS: newConfigs
	});
};

// ─── OpenAI Connections ─────────────────────────────────

/**
 * Add an OpenAI-compatible API connection at the system level.
 * Mirrors the logic in admin/Settings/Connections.svelte.
 */
export const addOpenAIConnection = async (
	token: string,
	connection: { url: string; key?: string; config?: object }
) => {
	const normalizedUrl = normalizeConnectionUrl(connection.url);
	const isLocalLlama = isAuraProLocalLlamaUrl(normalizedUrl);
	const current = isLocalLlama
		? await normalizeAuraProLocalLlamaConnections(token, normalizedUrl)
		: await getOpenAIConfig(token);
	const urls = [...(current?.OPENAI_API_BASE_URLS ?? [])];
	const keys = [...(current?.OPENAI_API_KEYS ?? [])];
	const configs = { ...(current?.OPENAI_API_CONFIGS ?? {}) };

	// Don't add duplicates. Local llama.cpp connections have already been
	// normalized above, including old automatically incremented ports.
	if (urls.map((u: string) => normalizeConnectionUrl(u)).includes(normalizedUrl)) {
		return current;
	}

	urls.push(normalizedUrl);
	keys.push(connection.key ?? '');
	if (connection.config || isLocalLlama) {
		configs[(urls.length - 1).toString()] = {
			...(connection.config ?? {}),
			...(isLocalLlama ? { connection_type: 'local', provider: 'llama.cpp' } : {})
		};
	}

	return await updateOpenAIConfig(token, {
		ENABLE_OPENAI_API: current?.ENABLE_OPENAI_API ?? true,
		OPENAI_API_BASE_URLS: urls,
		OPENAI_API_KEYS: keys,
		OPENAI_API_CONFIGS: configs
	});
};

/**
 * Remove an OpenAI-compatible API connection by URL at the system level.
 * Re-indexes OPENAI_API_CONFIGS to match the admin delete pattern.
 */
export const removeOpenAIConnection = async (token: string, url: string) => {
	const current = await getOpenAIConfig(token);
	const urls: string[] = current?.OPENAI_API_BASE_URLS ?? [];
	const keys: string[] = current?.OPENAI_API_KEYS ?? [];
	const configs: Record<string, any> = current?.OPENAI_API_CONFIGS ?? {};

	const normalizedUrl = url.replace(/\/$/, '');
	const idx = urls.findIndex((u: string) => u.replace(/\/$/, '') === normalizedUrl);
	if (idx === -1) return current;

	const newUrls = urls.filter((_: string, i: number) => i !== idx);
	const newKeys = keys.filter((_: string, i: number) => i !== idx);

	// Re-index configs (mirrors admin/Settings/Connections.svelte onDelete)
	const newConfigs: Record<string, any> = {};
	newUrls.forEach((_: string, newIdx: number) => {
		newConfigs[newIdx] = configs[newIdx < idx ? newIdx : newIdx + 1];
	});

	return await updateOpenAIConfig(token, {
		ENABLE_OPENAI_API: current?.ENABLE_OPENAI_API ?? true,
		OPENAI_API_BASE_URLS: newUrls,
		OPENAI_API_KEYS: newKeys,
		OPENAI_API_CONFIGS: newConfigs
	});
};

// ─── Terminal Server Connections ────────────────────────

/**
 * Add a terminal server connection at the system level.
 * Mirrors the logic in admin/Settings/Integrations.svelte.
 */
export const addTerminalConnection = async (
	token: string,
	connection: { url: string; key?: string; name?: string; auth_type?: string }
) => {
	const current = await getTerminalServerConnections(token);
	const servers = current?.TERMINAL_SERVER_CONNECTIONS ?? [];

	// Don't add duplicates
	if (servers.find((s: any) => s.url === connection.url)) {
		return current;
	}

	servers.push({
		url: connection.url,
		key: connection.key ?? '',
		auth_type: connection.auth_type ?? 'bearer',
		name: connection.name ?? 'Open Terminal',
		enabled: true
	});

	return await setTerminalServerConnections(token, {
		TERMINAL_SERVER_CONNECTIONS: servers
	});
};

/**
 * Remove a terminal server connection by URL at the system level.
 */
export const removeTerminalConnection = async (token: string, url: string) => {
	const current = await getTerminalServerConnections(token);
	const servers = current?.TERMINAL_SERVER_CONNECTIONS ?? [];

	const filtered = servers.filter((s: any) => s.url !== url);
	if (filtered.length === servers.length) return current; // nothing to remove

	return await setTerminalServerConnections(token, {
		TERMINAL_SERVER_CONNECTIONS: filtered
	});
};
