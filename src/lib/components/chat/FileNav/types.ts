export type FileEntry = {
	name: string;
	type: 'file' | 'directory';
	size?: number;
	modified?: number;
};
