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

	it.each([undefined, 0, 1])('normalizes glossary candidates for chunk %s', async (chunkIndex) => {
		const fetchMock = vi.fn(async (_url: string, request: RequestInit) => {
			const body = request.body as FormData;
			expect(body.has('language')).toBe(false);
			expect(body.get('language_candidates')).toBe('zh,ne');
			expect(body.get('chunk_index')).toBe(chunkIndex === undefined ? null : String(chunkIndex));
			expect(body.get('reset_stream')).toBe(
				chunkIndex === undefined ? null : String(chunkIndex === 0)
			);
			return Response.json({ text: 'Buenos dias' });
		});
		vi.stubGlobal('fetch', fetchMock);

		await transcribeAudio('token', new File(['audio'], 'sample.wav'), {
			languageCandidates: ['zh', '尼泊尔语'],
			chunkIndex,
			resetStream: chunkIndex === undefined ? undefined : chunkIndex === 0
		});
		expect(fetchMock).toHaveBeenCalledOnce();
	});

	it.each([
		[['中文', '尼泊爾語', 'ne-NP', '自訂字典名稱'], 'zh,ne'],
		[['印地語', 'Bengali', '孟加拉语'], 'hi,bn'],
		['Filipino,他加祿語,fil-PH,es-MX', 'tl,es'],
		[['祖鲁语', '愛爾蘭文', 'Lao', '道格里文', '索馬里文'], 'zu,ga,lo,doi,so'],
		['["中文", "Nepali", null]', 'zh,ne'],
		['zh ne', 'zh,ne'],
		[['自動', 'und', '不知道'], null]
	])('sends unique speech codes for %j', async (languageCandidates, expected) => {
		const fetchMock = vi.fn(async (_url: string, request: RequestInit) => {
			const body = request.body as FormData;
			expect(body.get('language_candidates')).toBe(expected);
			return Response.json({ text: '' });
		});
		vi.stubGlobal('fetch', fetchMock);

		await transcribeAudio('token', new File(['audio'], 'sample.wav'), { languageCandidates });
		expect(fetchMock).toHaveBeenCalledOnce();
	});
});
