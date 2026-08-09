import { describe, expect, it, vi } from 'vitest';

import { readKnowledgeImportProgress } from './index';

const streamResponse = (...chunks: string[]) =>
	new Response(
		new ReadableStream({
			start(controller) {
				const encoder = new TextEncoder();
				for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
				controller.close();
			}
		})
	);

describe('readKnowledgeImportProgress', () => {
	it('handles fragmented progress and result events', async () => {
		const onProgress = vi.fn();
		const response = streamResponse(
			'{"type":"progress","stage":"extracting_',
			'epub","progress":20}\n{"type":"heartbeat"}\n',
			'{"type":"result","result":{"status":true}}\n'
		);

		await expect(readKnowledgeImportProgress(response, onProgress)).resolves.toEqual({
			status: true
		});
		expect(onProgress).toHaveBeenCalledOnce();
		expect(onProgress).toHaveBeenCalledWith(
			expect.objectContaining({ stage: 'extracting_epub', progress: 20 })
		);
	});

	it('surfaces streamed backend errors', async () => {
		const response = streamResponse('{"type":"error","error":"Embedding failed"}\n');

		await expect(readKnowledgeImportProgress(response, vi.fn())).rejects.toThrow(
			'Embedding failed'
		);
	});

	it('rejects a stream that ends without a result', async () => {
		const response = streamResponse('{"type":"heartbeat"}\n');

		await expect(readKnowledgeImportProgress(response, vi.fn())).rejects.toThrow(
			'Knowledge import ended without a result'
		);
	});
});
