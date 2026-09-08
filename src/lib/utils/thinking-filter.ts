export function getThinkingFilter<T extends { id: string; name?: string }>(filters: T[]): T | null {
	const byId = filters.find((filter) => filter.id.toLowerCase() === 'thinking');
	if (byId) return byId;
	const byName = filters.filter((filter) => filter.name?.trim().toLowerCase() === 'thinking');
	return byName.length === 1 ? byName[0] : null;
}

export function toggleThinkingFilter(selectedIds: string[], id: string): string[] {
	return selectedIds.includes(id)
		? selectedIds.filter((selectedId) => selectedId !== id)
		: [...selectedIds, id];
}
