export type GlossaryRuntimeSettings = {
	token_limit?: number;
	kv_cache_type?: 'q8_0' | 'q4_0' | 'f16';
};

export const mergeGlossaryRuntimeSettings = <T extends GlossaryRuntimeSettings>(
	draft: T,
	previous: GlossaryRuntimeSettings,
	incoming: GlossaryRuntimeSettings
): T => ({
	...draft,
	token_limit:
		draft.token_limit === previous.token_limit ? incoming.token_limit : draft.token_limit,
	kv_cache_type:
		draft.kv_cache_type === previous.kv_cache_type ? incoming.kv_cache_type : draft.kv_cache_type
});
