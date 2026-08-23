import { WEBUI_API_BASE_URL } from '$lib/constants';

export type OpenCodeAgent = string;

export type OpenCodeChatConfig = {
	enabled: boolean;
	directory: string;
	agent: OpenCodeAgent;
	model: string;
};

export type OpenCodeAgentOption = {
	id: string;
	name: string;
	description: string;
	mode: string;
};

export type OpenCodeModelOption = {
	id: string;
	provider_id: string;
	provider_name: string;
	model_id: string;
	name: string;
	default: boolean;
};

export type OpenCodeVcs = {
	branch: string;
	root: string;
};

export type OpenCodeTodo = {
	id: string;
	content: string;
	status: string;
	priority: string;
};

export type OpenCodeDiffSource = 'session' | 'agent_actions' | 'workspace_status' | 'none';

export type OpenCodeCapabilities = {
	directory: string;
	agents: OpenCodeAgentOption[];
	models: OpenCodeModelOption[];
	default_model: string;
	vcs: OpenCodeVcs;
};

export type OpenCodeStatus = {
	available: boolean;
	version?: string | null;
	default_directory?: string | null;
	error?: string;
	session?: {
		id?: string | null;
		directory?: string | null;
		agent?: OpenCodeAgent;
		model?: string | null;
	};
};

export type OpenCodeWorkspace = {
	session_id: string;
	message_id?: string | null;
	directory: string;
	agent: OpenCodeAgent;
	model: string;
	status?: Record<string, unknown> | string | null;
	todos: OpenCodeTodo[];
	vcs: OpenCodeVcs;
	files: Record<string, unknown>[];
	diffs: Record<string, unknown>[];
	diff_source: OpenCodeDiffSource;
};

const request = async <T>(token: string, path: string, init: RequestInit = {}): Promise<T> => {
	const response = await fetch(`${WEBUI_API_BASE_URL}/opencode${path}`, {
		...init,
		headers: {
			Accept: 'application/json',
			Authorization: `Bearer ${token}`,
			...(init.body ? { 'Content-Type': 'application/json' } : {}),
			...(init.headers ?? {})
		}
	});
	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: response.statusText }));
		throw error?.detail ?? error?.error ?? response.statusText;
	}
	return response.json();
};

export const getOpenCodeStatus = async (
	token: string,
	chatId?: string | null
): Promise<OpenCodeStatus> =>
	request(token, `/status${chatId ? `?chat_id=${encodeURIComponent(chatId)}` : ''}`);

export const validateOpenCodeDirectory = async (
	token: string,
	directory: string
): Promise<{ valid: boolean; directory: string }> =>
	request(token, '/directory/validate', {
		method: 'POST',
		body: JSON.stringify({ directory })
	});

export const abortOpenCodeChat = async (
	token: string,
	chatId: string
): Promise<{ aborted: boolean }> =>
	request(token, `/chats/${encodeURIComponent(chatId)}/abort`, { method: 'POST' });

const messageQuery = (messageId?: string | null): string =>
	messageId ? `?message_id=${encodeURIComponent(messageId)}` : '';

export const getOpenCodeCapabilities = async (
	token: string,
	directory: string
): Promise<OpenCodeCapabilities> =>
	request(token, '/capabilities', {
		method: 'POST',
		body: JSON.stringify({ directory })
	});

export const getOpenCodeChatDiff = async (
	token: string,
	chatId: string,
	messageId?: string | null
) =>
	request<{ items: Record<string, unknown>[] }>(
		token,
		`/chats/${encodeURIComponent(chatId)}/diff${messageQuery(messageId)}`
	);

export const getOpenCodeWorkspace = async (
	token: string,
	chatId: string,
	messageId?: string | null
): Promise<OpenCodeWorkspace> =>
	request(token, `/chats/${encodeURIComponent(chatId)}/workspace${messageQuery(messageId)}`);

export const resetOpenCodeSession = async (
	token: string,
	chatId: string
): Promise<{ reset: boolean }> =>
	request(token, `/chats/${encodeURIComponent(chatId)}/session/reset`, { method: 'POST' });

export const revertOpenCodeMessage = async (
	token: string,
	chatId: string,
	messageId: string
): Promise<{ reverted: boolean }> =>
	request(token, `/chats/${encodeURIComponent(chatId)}/revert`, {
		method: 'POST',
		body: JSON.stringify({ message_id: messageId })
	});

export const unrevertOpenCodeChat = async (
	token: string,
	chatId: string
): Promise<{ restored: boolean }> =>
	request(token, `/chats/${encodeURIComponent(chatId)}/unrevert`, { method: 'POST' });
