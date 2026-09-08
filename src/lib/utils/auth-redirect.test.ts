import { describe, expect, it } from 'vitest';
import { getPostSignInPath } from './auth-redirect';

describe('post sign-in destination', () => {
	it.each([null, undefined, '', '/auth', '/auth/', '/auth?redirect=%2Fauth'])(
		'avoids returning to the login page: %s',
		(path) => expect(getPostSignInPath(path)).toBe('/')
	);
	it('preserves the intended conversation and query', () => {
		expect(getPostSignInPath('/c/example?model=local#message')).toBe(
			'/c/example?model=local#message'
		);
	});
	it.each(['https://example.com', '//example.com', '/\\example.com'])(
		'keeps navigation within the app: %s',
		(path) => expect(getPostSignInPath(path)).toBe('/')
	);
});
