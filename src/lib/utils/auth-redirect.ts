export const getPostSignInPath = (path: string | null | undefined): string => {
	if (!path?.startsWith('/') || path.startsWith('//')) return '/';
	try {
		const url = new URL(path, 'https://webui.invalid');
		if (url.origin !== 'https://webui.invalid' || /^\/auth\/?$/.test(url.pathname)) return '/';
		return `${url.pathname}${url.search}${url.hash}`;
	} catch {
		return '/';
	}
};
