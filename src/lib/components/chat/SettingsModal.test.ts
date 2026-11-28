import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { parse } from 'svelte/compiler';
import ts from 'typescript';
import { describe, expect, it, vi } from 'vitest';

const component = readFileSync(new URL('./SettingsModal.svelte', import.meta.url), 'utf8');
const { start, end } = parse(component).instance!.content;
const source = ts.createSourceFile(
	'SettingsModal.ts',
	component.slice(start, end),
	ts.ScriptTarget.Latest,
	true
);
const declarations = new Set(['connectionRefresh', 'pendingSettingsSave', 'saveSettings']);
const code = source.statements
	.filter(
		(statement) =>
			ts.isVariableStatement(statement) &&
			statement.declarationList.declarations.some((declaration) =>
				declarations.has(declaration.name.getText(source))
			)
	)
	.map((statement) => statement.getText(source))
	.join('\n');

const setup = (directConnectionsEnabled = true) => {
	let currentSettings = {};
	const updateUserSettings = vi.fn(async (_token: string, _payload: object) => undefined);
	const getModels = vi.fn<() => Promise<string[]>>(async () => ['local']);
	const setModels = vi.fn();
	const context = {
		$config: { features: { enable_direct_connections: directConnectionsEnabled } },
		get $settings() {
			return currentSettings;
		},
		localStorage: { token: 'session' },
		settings: { set: (next: object) => (currentSettings = next) },
		models: { set: setModels },
		updateUserSettings,
		getModels,
		console: { warn: vi.fn() }
	};
	const save = runInNewContext(`${code}\nsaveSettings`, context) as (
		updated: object
	) => Promise<void>;
	return { save, updateUserSettings, getModels, setModels, warn: context.console.warn };
};

describe('personal settings saves', () => {
	it('does not request models for ordinary settings or when direct connections are disabled', async () => {
		const ordinary = setup();
		await ordinary.save({ speechAutoSend: true });
		expect(ordinary.updateUserSettings).toHaveBeenCalledWith('session', {
			ui: { speechAutoSend: true }
		});
		expect(ordinary.getModels).not.toHaveBeenCalled();

		const disabled = setup(false);
		await disabled.save({ directConnections: null });
		expect(disabled.getModels).not.toHaveBeenCalled();
	});

	it('saves connections without waiting for models and ignores a stale model response', async () => {
		const f = setup();
		let finishOldRequest!: (models: string[]) => void;
		f.getModels.mockImplementationOnce(
			() => new Promise((resolve) => (finishOldRequest = resolve))
		);
		await f.save({ directConnections: { name: 'previous' } });
		expect(f.updateUserSettings).toHaveBeenCalledTimes(1);
		expect(f.setModels).not.toHaveBeenCalled();
		await f.save({ directConnections: { name: 'current' } });
		await vi.waitFor(() => expect(f.setModels).toHaveBeenCalledWith(['local']));
		finishOldRequest(['old']);
		await Promise.resolve();
		expect(f.setModels).toHaveBeenCalledTimes(1);
	});

	it('keeps the saved settings when the model refresh fails', async () => {
		const f = setup();
		f.getModels.mockRejectedValueOnce(new Error('offline'));
		await f.save({ directConnections: null });
		await vi.waitFor(() => expect(f.warn).toHaveBeenCalled());
		expect(f.updateUserSettings).toHaveBeenCalledTimes(1);
		await f.save({ speechAutoSend: true });
		expect(f.updateUserSettings).toHaveBeenLastCalledWith('session', {
			ui: { directConnections: null, speechAutoSend: true }
		});
	});

	it('persists rapid saves in order and does not block subsequent saves after a failure', async () => {
		const f = setup();
		let rejectFirst!: (error: Error) => void;
		f.updateUserSettings.mockImplementationOnce(
			() => new Promise((_, reject) => (rejectFirst = reject))
		);
		const first = expect(f.save({ speechAutoSend: true })).rejects.toThrow('save failed');
		const second = f.save({ speechAutoSend: false });
		await vi.waitFor(() => expect(f.updateUserSettings).toHaveBeenCalledTimes(1));
		rejectFirst(new Error('save failed'));
		await Promise.all([first, second]);
		expect(f.updateUserSettings).toHaveBeenLastCalledWith('session', {
			ui: { speechAutoSend: false }
		});
	});
});
