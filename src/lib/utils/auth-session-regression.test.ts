import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { parse } from 'svelte/compiler';
import { expect, it, vi } from 'vitest';
import { getPostSignInPath } from './auth-redirect';

// Exercise the route's actual guards without mounting its unrelated login widgets.
const script = (relative: string) => {
	const source = readFileSync(new URL(relative, import.meta.url), 'utf8');
	const content = parse(source).instance!.content;
	return ts.createSourceFile(
		'route.ts',
		source.slice(content.start, content.end),
		ts.ScriptTarget.Latest,
		true
	);
};
const authScript = script('../../routes/auth/+page.svelte');
const mount = authScript.statements.find(
	(node) =>
		ts.isExpressionStatement(node) &&
		ts.isCallExpression(node.expression) &&
		node.expression.expression.getText(authScript) === 'onMount'
) as ts.ExpressionStatement;
const callback = (mount.expression as ts.CallExpression).arguments[0] as ts.ArrowFunction;
const guard = (callback.body as ts.Block).statements
	.slice(0, 2)
	.map((node) => node.getText(authScript))
	.join('\n');
const runGuard = new Function(
	'$user',
	'$page',
	'goto',
	'getPostSignInPath',
	'localStorage',
	`return (async () => { ${guard}; return 'login'; })();`
);

it.each([null, undefined])(
	'keeps an unauthenticated user on the login page (%s)',
	async (session) => {
		const navigate = vi.fn();
		expect(
			await runGuard(
				session,
				{ url: new URL('https://local/auth?redirect=%2Fc%2Fexample') },
				navigate,
				getPostSignInPath,
				{ setItem: vi.fn() }
			)
		).toBe('login');
		expect(navigate).not.toHaveBeenCalled();
	}
);

it('redirects a signed-in user once, without running login initialization', async () => {
	const navigate = vi.fn();
	expect(
		await runGuard(
			{ id: 'user' },
			{ url: new URL('https://local/auth?redirect=%2Fc%2Fexample') },
			navigate,
			getPostSignInPath,
			{}
		)
	).toBeUndefined();
	expect(navigate).toHaveBeenCalledExactlyOnceWith('/c/example');
});

const rootScript = script('../../routes/+layout.svelte');
let fetchWrapper: ts.ArrowFunction;
const visit = (node: ts.Node) => {
	if (
		ts.isBinaryExpression(node) &&
		node.left.getText(rootScript) === 'window.fetch' &&
		ts.isArrowFunction(node.right)
	)
		fetchWrapper = node.right;
	ts.forEachChild(node, visit);
};
visit(rootScript);
const makeFetch = new Function(
	'originalFetch',
	'localStorage',
	'isAuthenticatedBackendFetch',
	'isCurrentSessionUnauthorized',
	'redirectToAuthAfterUnauthorized',
	`return (${fetchWrapper!.getText(rootScript)});`
);

it('does not invalidate a new login when an old session check completes late', async () => {
	const storage = { token: 'expired' };
	let resolveCheck!: (value: boolean) => void;
	const check = vi.fn(
		() =>
			new Promise<boolean>((resolve) => {
				resolveCheck = resolve;
			})
	);
	const redirect = vi.fn();
	const fetch = makeFetch(
		async () => ({ status: 401 }),
		storage,
		() => true,
		check,
		redirect
	);
	const pending = fetch('/api/v1/models');
	await Promise.resolve();
	expect(check).toHaveBeenCalled();
	storage.token = 'new-session';
	resolveCheck(true);
	await pending;
	expect(redirect).not.toHaveBeenCalled();
});

it('still redirects when the current session is genuinely expired', async () => {
	const redirect = vi.fn();
	const fetch = makeFetch(
		async () => ({ status: 401 }),
		{ token: 'expired' },
		() => true,
		async () => true,
		redirect
	);
	await fetch('/api/v1/models');
	expect(redirect).toHaveBeenCalledTimes(1);
});
