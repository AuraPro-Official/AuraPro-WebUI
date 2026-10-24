import { writable } from 'svelte/store';

const params = new URLSearchParams(location.search);
export const WEBUI_NAME = writable('AuraPro');
export const user = writable({ id: 'reader', role: params.get('role') || 'admin' });
export const mobile = writable(innerWidth < 768);
export const showSidebar = writable(params.get('sidebar') === '1');
export const showArchivedChats = writable(false);
