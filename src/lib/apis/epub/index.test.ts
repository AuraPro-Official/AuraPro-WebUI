import { afterEach, describe, expect, it, vi } from 'vitest';

import {
	createEpubBatchDraft,
	createEpubSectionGraphBatchDraft,
	getEpubBatchJob,
	getEpubBatchJobs,
	getEpubBooks,
	getEpubPromptProfiles,
	getEpubRelationAssertions,
	getEpubSampleBatchReviews,
	importEpub,
	indexEpubVersion,
	reviewEpubRelationAssertion,
	reviewEpubSampleBatch,
	recoverEpubBatches,
	searchEpub
} from './index';

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
					fused_results: [],
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
			expect.objectContaining({
				headers: expect.objectContaining({ authorization: 'Bearer token-1' })
			})
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
			.mockResolvedValueOnce(
				jsonResponse({ batch_job_id: 'batch-1', item_count: 2, status: 'DRAFT' })
			)
			.mockResolvedValueOnce(jsonResponse({ created: true, version_id: 'version-1' }));
		vi.stubGlobal('fetch', fetchMock);

		await createEpubBatchDraft('admin-token', {
			version_id: 'version-1',
			profile_name: 'server-profile',
			is_sample: true,
			sample_limit: 10
		});
		await importEpub(
			'admin-token',
			new File(['epub'], 'book.epub', { type: 'application/epub+zip' })
		);

		expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/epub/admin/batches');
		expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/epub/admin/import');
		expect(fetchMock.mock.calls[1][1].body).toBeInstanceOf(FormData);
	});

	it('uses separate admin endpoints for a section graph draft and relation review', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(
				jsonResponse({ batch_job_id: 'graph-1', item_count: 1, status: 'DRAFT' })
			)
			.mockResolvedValueOnce(jsonResponse({ total: 1, offset: 0, items: [] }))
			.mockResolvedValueOnce(jsonResponse({ assertion_id: 'assertion-1', status: 'APPROVED' }));
		vi.stubGlobal('fetch', fetchMock);

		await createEpubSectionGraphBatchDraft('admin-token', {
			version_id: 'version-1',
			profile_name: 'server-profile',
			is_sample: true,
			sample_limit: 10
		});
		await getEpubRelationAssertions('admin-token', {
			status: 'PROVISIONAL',
			version_id: 'version-1'
		});
		await reviewEpubRelationAssertion('admin-token', 'assertion-1', 'APPROVED');

		expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/epub/admin/section-graph-batches');
		expect(fetchMock.mock.calls[1][0]).toContain('/api/v1/epub/admin/relation-assertions?');
		expect(fetchMock.mock.calls[2][0]).toBe('/api/v1/epub/admin/relation-assertions/assertion-1');
		expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: 'PUT' });
	});

	it('uses administrator-only sample review endpoints for the full Batch quality gate', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(jsonResponse({ items: [] }))
			.mockResolvedValueOnce(
				jsonResponse({
					sample_batch_job_id: 'sample-1',
					version_id: 'version-1',
					job_kind: 'SECTION_GRAPH',
					status: 'APPROVED',
					reviewed_by: 'administrator',
					reviewed_at: '2026-01-01T00:00:00Z',
					batch_status: 'SUCCEEDED'
				})
			);
		vi.stubGlobal('fetch', fetchMock);

		await getEpubSampleBatchReviews('admin-token', {
			version_id: 'version-1',
			job_kind: 'SECTION_GRAPH'
		});
		await reviewEpubSampleBatch('admin-token', 'sample-1', 'APPROVED');

		expect(fetchMock.mock.calls[0][0]).toContain('/api/v1/epub/admin/sample-batch-reviews?');
		expect(fetchMock.mock.calls[0][0]).toContain('version_id=version-1');
		expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/epub/admin/sample-batches/sample-1/review');
		expect(fetchMock.mock.calls[1][1]).toMatchObject({
			method: 'PUT',
			body: JSON.stringify({ status: 'APPROVED' })
		});
	});

	it('surfaces an actionable API detail instead of hiding a failed authorization or configuration', async () => {
		vi.stubGlobal(
			'fetch',
			vi.fn().mockResolvedValue(jsonResponse({ detail: 'administrator required' }, 403))
		);
		await expect(getEpubBooks('ordinary-token')).rejects.toThrow('administrator required');
	});

	it('targets the server-owned version bulk-index endpoint and makes rebuild explicit', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			jsonResponse({
				version_id: 'version-1',
				mode: 'REBUILD',
				total_retrieval_units: 3,
				selected_retrieval_units: 3,
				skipped_ready: 0,
				ready: 3,
				degraded: 0,
				failed: 0,
				error_count: 0,
				errors: []
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		await indexEpubVersion('admin-token', 'version-1', true);

		expect(fetchMock).toHaveBeenCalledWith(
			'/api/v1/epub/admin/versions/version-1/index',
			expect.objectContaining({ method: 'POST', body: JSON.stringify({ rebuild: true }) })
		);
	});

	it('reads the selectable prompt profiles from the server instead of a hardcoded list', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			jsonResponse({
				prompt_profiles: ['zh-glossary-v1', 'zh-glossary-v2', 'zh-glossary-v3', 'zh-glossary-v4'],
				default_prompt_profile: 'zh-glossary-v4'
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const profiles = await getEpubPromptProfiles('admin-token');

		expect(profiles.default_prompt_profile).toBe('zh-glossary-v4');
		expect(profiles.prompt_profiles).toContain('zh-glossary-v4');
		expect(fetchMock).toHaveBeenCalledWith(
			'/api/v1/epub/admin/prompt-profiles',
			expect.objectContaining({
				headers: expect.objectContaining({ authorization: 'Bearer admin-token' })
			})
		);
	});

	it('uses lifecycle-only Batch history and recovery endpoints', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(jsonResponse({ total: 1, offset: 0, items: [] }))
			.mockResolvedValueOnce(jsonResponse({ batch_job_id: 'batch-1', items: [] }))
			.mockResolvedValueOnce(jsonResponse({ recovered: [], skipped: [] }));
		vi.stubGlobal('fetch', fetchMock);

		await getEpubBatchJobs('admin-token', { version_id: 'version-1' });
		await getEpubBatchJob('admin-token', 'batch-1');
		await recoverEpubBatches('admin-token');

		expect(fetchMock.mock.calls[0][0]).toContain('/api/v1/epub/admin/batches?');
		expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/epub/admin/batches/batch-1');
		expect(fetchMock.mock.calls[2][0]).toBe('/api/v1/epub/admin/batches/recover');
		expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: 'POST' });
	});
});
