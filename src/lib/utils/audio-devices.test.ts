import { describe, expect, it } from 'vitest';

import {
	groupAudioDevices,
	hasAnonymousAudioDevices,
	reconcileAudioDeviceId
} from './audio-devices';

const device = (kind: MediaDeviceKind, deviceId: string, label: string) =>
	({ kind, deviceId, label, groupId: '', toJSON: () => ({}) }) as MediaDeviceInfo;

describe('audio devices', () => {
	it('groups and deduplicates microphone and speaker devices', () => {
		const microphone = device('audioinput', 'mic-1', 'Built-in Microphone');
		const speaker = device('audiooutput', 'speaker-1', 'Built-in Speaker');
		const result = groupAudioDevices([
			microphone,
			microphone,
			speaker,
			device('videoinput', 'camera-1', 'Camera')
		]);

		expect(result.inputs).toEqual([microphone]);
		expect(result.outputs).toEqual([speaker]);
	});

	it('detects hidden labels and resets unavailable saved devices', () => {
		const microphone = device('audioinput', 'mic-1', '');
		const devices = { inputs: [microphone], outputs: [] };

		expect(hasAnonymousAudioDevices(devices)).toBe(true);
		expect(reconcileAudioDeviceId('mic-1', devices.inputs)).toBe('mic-1');
		expect(reconcileAudioDeviceId('old-mic', devices.inputs)).toBe('');
	});
});
