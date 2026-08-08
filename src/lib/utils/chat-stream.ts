type SocketSession = {
	id?: string;
	connected?: boolean;
	once: (event: 'connect', listener: () => void) => unknown;
	off: (event: 'connect', listener: () => void) => unknown;
	connect?: () => unknown;
};

const getConnectedSessionId = (socket: SocketSession | null | undefined) =>
	socket?.connected && socket.id ? socket.id : null;

export const waitForSocketSession = async (
	socket: SocketSession | null | undefined,
	timeoutMs = 5000
): Promise<string | null> => {
	const connectedSessionId = getConnectedSessionId(socket);
	if (connectedSessionId || !socket || timeoutMs <= 0) {
		return connectedSessionId;
	}

	return await new Promise((resolve) => {
		let timer: ReturnType<typeof setTimeout> | null = null;

		const finish = (sessionId: string | null) => {
			socket.off('connect', handleConnect);
			if (timer !== null) clearTimeout(timer);
			resolve(sessionId);
		};
		const handleConnect = () => finish(getConnectedSessionId(socket));

		socket.once('connect', handleConnect);
		timer = setTimeout(() => finish(null), timeoutMs);

		try {
			socket.connect?.();
		} catch {
			finish(null);
		}
	});
};

export const isChatEventForCurrentConversation = (
	eventChatId: unknown,
	currentChatId: string | null | undefined,
	eventMessageId: unknown,
	messages: Record<string, unknown> | null | undefined
) => {
	if (eventChatId === currentChatId) {
		return true;
	}

	return (
		!currentChatId &&
		typeof eventChatId === 'string' &&
		eventChatId.length > 0 &&
		typeof eventMessageId === 'string' &&
		Object.hasOwn(messages ?? {}, eventMessageId)
	);
};

export const isInactiveEventForQueueRun = (
	responseMessageIds: ReadonlySet<string>,
	eventMessageId: unknown
) =>
	typeof eventMessageId === 'string' &&
	eventMessageId.length > 0 &&
	responseMessageIds.has(eventMessageId);
export const hasPendingCurrentAssistantResponses = (history: {
	currentId?: string | null;
	messages?: Record<
		string,
		| {
				role?: string;
				parentId?: string | null;
				childrenIds?: string[];
				done?: boolean;
		  }
		| undefined
	>;
}) => {
	const messages = history?.messages ?? {};
	const currentId = history?.currentId;
	const currentMessage = currentId ? messages[currentId] : null;
	if (!currentMessage) return false;

	const responseParent =
		currentMessage.role === 'assistant' && currentMessage.parentId
			? messages[currentMessage.parentId]
			: currentMessage.role === 'user'
				? currentMessage
				: null;
	const responseIds =
		responseParent?.role === 'user'
			? (responseParent.childrenIds ?? [])
			: currentMessage.role === 'assistant' && currentId
				? [currentId]
				: [];

	return responseIds.some((messageId) => {
		const message = messages[messageId];
		return message?.role === 'assistant' && message.done !== true;
	});
};
