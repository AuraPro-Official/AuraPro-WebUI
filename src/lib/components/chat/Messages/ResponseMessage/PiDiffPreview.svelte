<script lang="ts">
	export let patch = '';
	$: rows = (() => {
		let oldLine = 0,
			newLine = 0;
		let inHunk = false;
		return patch.split('\n').map((text) => {
			const hunk = text.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
			if (hunk) {
				oldLine = Number(hunk[1]);
				newLine = Number(hunk[2]);
				inHunk = true;
			}
			const header = !!hunk || !inHunk || text.startsWith('\\') || !text;
			const added = !header && text.startsWith('+');
			const deleted = !header && text.startsWith('-');
			const row = {
				text,
				old: header || added ? '' : String(oldLine),
				next: header || deleted ? '' : String(newLine),
				added,
				deleted,
				header
			};
			if (!header && text) {
				if (!added) oldLine++;
				if (!deleted) newLine++;
			}
			return row;
		});
	})();
</script>

<div
	class="max-h-96 overflow-auto border-t border-gray-100 bg-gray-50 py-2 font-mono text-[11px] leading-5 dark:border-gray-800 dark:bg-gray-900"
>
	{#each rows as row}
		<div
			class="flex min-w-max whitespace-pre {row.added
				? 'bg-green-50 text-green-800 dark:bg-green-950/30 dark:text-green-300'
				: row.deleted
					? 'bg-red-50 text-red-800 dark:bg-red-950/30 dark:text-red-300'
					: row.header
						? 'text-blue-700 dark:text-blue-300'
						: ''}"
		>
			<span class="w-12 shrink-0 select-none pr-2 text-right opacity-50" title="原文件行号"
				>{row.old}</span
			>
			<span
				class="w-12 shrink-0 select-none border-r border-gray-200 pr-2 text-right opacity-50 dark:border-gray-700"
				title="修改后行号">{row.next}</span
			>
			<span class="px-3">{row.text || ' '}</span>
		</div>
	{/each}
</div>
