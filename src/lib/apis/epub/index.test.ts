import { afterEach, describe, expect, it, vi } from 'vitest';

import {
	applyEpubOverlay,
	createEpubBatchDraft,
	createEpubSectionGraphBatchDraft,
	getEpubBatchJob,
	getEpubBatchJobs,
	getEpubBooks,
	getEpubConcepts,
	getEpubPromptProfiles,
	getEpubRelationAssertions,
	getEpubVersionOverlay,
	getEpubSampleBatchReviews,
	importEpub,
	indexEpubVersion,
	mergeEpubConcepts,
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

	it('reads the concept graph and merges a duplicate through administrator-only endpoints', async () => {
		const fetchMock = vi
			.fn()
			.mockResolvedValueOnce(
				jsonResponse({
					total: 2,
					offset: 0,
					items: [
						{
							concept_id: 'concept-1',
							canonical_name: '扰动源',
							definition: '',
							status: 'PROVISIONAL',
							aliases: ['扰动源'],
							mention_count: 3
						}
					]
				})
			)
			.mockResolvedValueOnce(
				jsonResponse({
					concept_merge_id: 'merge-1',
					target_concept_id: 'concept-1',
					source_concept_id: 'concept-2',
					source_canonical_name: '强扰动源',
					canonical_name: '扰动源',
					status: 'PROVISIONAL',
					merged_by: 'administrator',
					merged_at: '2026-01-01T00:00:00Z',
					moved_aliases: 2,
					moved_mentions: 1,
					duplicate_mentions: 0,
					repointed_relations: 0,
					folded_relations: 0,
					dropped_self_relations: 0
				})
			);
		vi.stubGlobal('fetch', fetchMock);

		await getEpubConcepts('admin-token', { status: 'PROVISIONAL', limit: 100 });
		const merged = await mergeEpubConcepts('admin-token', {
			target_concept_id: 'concept-1',
			source_concept_id: 'concept-2'
		});

		expect(merged.source_canonical_name).toBe('强扰动源');
		expect(fetchMock.mock.calls[0][0]).toBe(
			'/api/v1/epub/admin/concepts?status=PROVISIONAL&offset=0&limit=100'
		);
		expect(fetchMock.mock.calls[0][1]).toMatchObject({
			headers: expect.objectContaining({ authorization: 'Bearer admin-token' })
		});
		expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/epub/admin/concepts/merge');
		expect(fetchMock.mock.calls[1][1]).toMatchObject({
			method: 'POST',
			body: JSON.stringify({
				target_concept_id: 'concept-1',
				source_concept_id: 'concept-2'
			})
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

	it('downloads the exact overlay bytes with the digest an administrator publishes', async () => {
		// Deliberately not the shape `JSON.stringify` would produce: the digest
		// covers the server's canonical bytes, so the client must keep them.
		const canonical =
			'{"book_title":"共享书","concepts":[{"aliases":["TCP"],"canonical_name":"TCP","definition":"","key":"tcp","status":"APPROVED"}],"epub_sha256":"a1","mentions":[],"overlay_format_version":1,"parser_version":"1","passage_fingerprint":{"count":2,"digest":"b2"},"relations":[]}';
		const fetchMock = vi.fn().mockResolvedValue(
			new Response(canonical, {
				status: 200,
				headers: { 'Content-Type': 'application/json', 'X-Overlay-SHA256': 'digest-1' }
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const download = await getEpubVersionOverlay('admin-token', 'version-1');

		expect(download.text).toBe(canonical);
		expect(download.overlay_sha256).toBe('digest-1');
		expect(download.overlay.concepts[0].key).toBe('tcp');
		// An artifact never carries passage text; only labels and locations.
		expect(download.overlay.mentions).toEqual([]);
		expect(fetchMock).toHaveBeenCalledWith(
			'/api/v1/epub/admin/versions/version-1/overlay',
			expect.objectContaining({
				headers: expect.objectContaining({ authorization: 'Bearer admin-token' })
			})
		);
	});

	it('uploads an overlay to the administrator-only apply endpoint', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			jsonResponse({
				version_id: 'version-1',
				epub_sha256: 'a1',
				overlay_format_version: 1,
				applied: 4,
				skipped: 1,
				rejected: 0,
				applied_detail: { concepts_created: 2, mentions_created: 2 },
				skipped_detail: { mentions_existing: 1 },
				skipped_reasons: { mention_admin_owned: 1 },
				rejection_reasons: {},
				uploaded_overlay_sha256: 'digest-1',
				canonical_overlay_sha256: 'digest-1',
				vectors_require_reindex: true
			})
		);
		vi.stubGlobal('fetch', fetchMock);

		const result = await applyEpubOverlay(
			'admin-token',
			new File(['{}'], 'overlay.json', { type: 'application/json' })
		);

		expect(result.applied).toBe(4);
		expect(result.vectors_require_reindex).toBe(true);
		expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/epub/admin/overlays');
		expect(fetchMock.mock.calls[0][1].method).toBe('POST');
		expect(fetchMock.mock.calls[0][1].body).toBeInstanceOf(FormData);
	});

	it('surfaces a refused overlay gate as its actionable reason class', async () => {
		// Each call needs its own Response: a body can only be read once.
		vi.stubGlobal(
			'fetch',
			vi
				.fn()
				.mockImplementation(async () =>
					jsonResponse(
						{ detail: 'passage_fingerprint_mismatch: this store’s passages differ' },
						400
					)
				)
		);

		await expect(applyEpubOverlay('admin-token', new File(['{}'], 'overlay.json'))).rejects.toThrow(
			'passage_fingerprint_mismatch'
		);
		await expect(getEpubVersionOverlay('admin-token', 'version-1')).rejects.toThrow(
			'passage_fingerprint_mismatch'
		);
	});
});
