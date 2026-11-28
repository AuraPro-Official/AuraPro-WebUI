import { afterEach, describe, expect, it, vi } from 'vitest';

import { transcribeAudio } from './index';

afterEach(() => vi.unstubAllGlobals());

describe('audio transcription requests', () => {
	it('leaves language selection to the service when there are no hints', async () => {
		const fetchMock = vi.fn(async (_url: string, request: RequestInit) => {
			const body = request.body as FormData;
			expect(body.has('language')).toBe(false);
			expect(body.has('language_candidates')).toBe(false);
			return Response.json({ text: 'Buenos dias' });
		});
		vi.stubGlobal('fetch', fetchMock);

		await expect(transcribeAudio('token', new File(['audio'], 'sample.wav'))).resolves.toEqual({
			text: 'Buenos dias'
		});
		expect(fetchMock).toHaveBeenCalledOnce();
	});

	it.each([undefined, 0, 1])('preserves glossary candidates for chunk %s', async (chunkIndex) => {
		const fetchMock = vi.fn(async (_url: string, request: RequestInit) => {
			const body = request.body as FormData;
			expect(body.has('language')).toBe(false);
			expect(body.get('language_candidates')).toBe('zh,es');
			expect(body.get('chunk_index')).toBe(chunkIndex === undefined ? null : String(chunkIndex));
			expect(body.get('reset_stream')).toBe(
				chunkIndex === undefined ? null : String(chunkIndex === 0)
			);
			return Response.json({ text: 'Buenos dias' });
		});
		vi.stubGlobal('fetch', fetchMock);

		await transcribeAudio('token', new File(['audio'], 'sample.wav'), {
			languageCandidates: ['zh', 'es'],
			chunkIndex,
			resetStream: chunkIndex === undefined ? undefined : chunkIndex === 0
		});
		expect(fetchMock).toHaveBeenCalledOnce();
	});
});
