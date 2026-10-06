export const piToolLabel = (name: string, i18n: { t: (key: string) => string }) => {
	const labels: Record<string, string> = {
		read: 'Read file',
		write: 'Write file',
		edit: 'Edit file',
		grep: 'Search file contents',
		find: 'Find files',
		ls: 'List directory',
		powershell: 'Run command',
		find_roots: 'Find windows',
		observe_ui: 'Observe window',
		search_ui: 'Find controls',
		expand_ui: 'Expand controls',
		inspect_ui: 'Inspect control',
		act_ui: 'Operate computer',
		read_text: 'Read text',
		wait_for: 'Wait for target',
		browser_navigate: 'Open webpage',
		browser_snapshot: 'Read webpage',
		browser_take_screenshot: 'Take screenshot',
		browser_click: 'Click webpage',
		browser_type: 'Enter webpage text',
		browser_tabs: 'Browser tabs',
		enable_extension_tools: 'Enable extension tools'
	};
	return labels[name] ? i18n.t(labels[name]) : name;
};

export const piStatusLabel = (value: string, i18n: { t: (key: string) => string }) =>
	i18n.t(
		(
			{ running: 'Running', completed: 'Completed', error: 'Failed', pending: 'Pending' } as Record<
				string,
				string
			>
		)[value] || value
	);
