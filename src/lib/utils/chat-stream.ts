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
