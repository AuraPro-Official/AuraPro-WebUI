export type AudioDeviceLists = {
	inputs: MediaDeviceInfo[];
	outputs: MediaDeviceInfo[];
};

const uniqueDevices = (devices: MediaDeviceInfo[]) => {
	const seen = new Set<string>();
	return devices.filter((device) => {
		const key = `${device.kind}:${device.deviceId}`;
		if (seen.has(key)) return false;
		seen.add(key);
		return true;
	});
};

export const groupAudioDevices = (devices: MediaDeviceInfo[]): AudioDeviceLists => ({
	inputs: uniqueDevices(devices.filter((device) => device.kind === 'audioinput')),
	outputs: uniqueDevices(devices.filter((device) => device.kind === 'audiooutput'))
});

export const hasAnonymousAudioDevices = ({ inputs, outputs }: AudioDeviceLists) =>
	[...inputs, ...outputs].some((device) => !device.label.trim());

export const reconcileAudioDeviceId = (deviceId: string, devices: MediaDeviceInfo[]) =>
	deviceId && devices.some((device) => device.deviceId === deviceId) ? deviceId : '';
