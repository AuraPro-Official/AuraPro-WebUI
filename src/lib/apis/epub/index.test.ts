import { afterEach, describe, expect, it, vi } from 'vitest';

import { createEpubBatchDraft, getEpubBooks, importEpub, searchEpub } from './index';

const jsonResponse = (body: unknown, status = 200) =>
	new Response(JSON.stringify(body), {
		status,
		headers: { 'Content-Type': 'application/json' }
	});

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('EPUB concept API client', () => {
	it('uses the authenticated shared-library read and search endpoints', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(jsonResponse([{ book_id: 'book-1', title: 'Shared book' }]))
			.mockResolvedValueOnce(
				jsonResponse({
					query: 'TCP',
					resolved_concepts: ['TCP'],
					graph_total: 1,
					graph_offset: 0,
					graph_results: [],
					vector_results: [],
					degraded: []
				})
			);
		vi.stubGlobal('fetch', fetchMock);

		await expect(getEpubBooks('token-1')).resolves.toMatchObject([{ book_id: 'book-1' }]);
		await expect(searchEpub('token-1', { query: 'TCP' })).resolves.toMatchObject({
			graph_total: 1
		});

		expect(fetchMock).toHaveBeenNthCalledWith(
			1,
			'/api/v1/epub/books',
			expect.objectContaining({ headers: expect.objectContaining({ authorization: 'Bearer token-1' }) })
		);
		expect(fetchMock).toHaveBeenNthCalledWith(
			2,
			'/api/v1/epub/search',
			expect.objectContaining({ method: 'POST' })
		);
	});

	it('keeps administrator mutations on their explicit server-owned endpoints', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(jsonResponse({ batch_job_id: 'batch-1', item_count: 2, status: 'DRAFT' }))
			.mockResolvedValueOnce(jsonResponse({ created: true, version_id: 'version-1' }));
		vi.stubGlobal('fetch', fetchMock);

		await createEpubBatchDraft('admin-token', {
			version_id: 'version-1',
			profile_name: 'server-profile',
			is_sample: true,
			sample_limit: 10
		});
		await importEpub('admin-token', new File(['epub'], 'book.epub', { type: 'application/epub+zip' }));

		expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/epub/admin/batches');
		expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/epub/admin/import');
		expect(fetchMock.mock.calls[1][1].body).toBeInstanceOf(FormData);
	});

	it('surfaces an actionable API detail instead of hiding a failed authorization or configuration', async () => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'administrator required' }, 403)));
		await expect(getEpubBooks('ordinary-token')).rejects.toThrow('administrator required');
	});
});
