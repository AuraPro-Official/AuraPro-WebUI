import { describe, expect, it } from 'vitest';
import { buildOutputDisplayItems, getOutputText, type OutputItem } from './structuredOutput';

describe('Agent transcript', () => {
	it('keeps progress messages between tool groups and excludes tool results from prose', () => {
		const output: OutputItem[] = [
			{ type: 'message', id: 'first', content: [{ text: 'Checking files.' }] },
			{ type: 'function_call', call_id: 'read', name: 'read', arguments: { path: 'test.txt' } },
			{ type: 'function_call_output', call_id: 'read', output: [{ text: 'File contents' }] },
			{ type: 'message', id: 'next', content: [{ text: 'Running checks.' }] },
			{ type: 'function_call', call_id: 'bash', name: 'bash', status: 'in_progress' },
			{
				type: 'function_call_output',
				call_id: 'bash',
				status: 'in_progress',
				output: [{ text: 'Partial output' }]
			}
		];
		const items = buildOutputDisplayItems(output);
		expect(items.map((item) => item.type)).toEqual([
			'message',
			'detail_single',
			'message',
			'detail_single'
		]);
		expect(items[1]).toMatchObject({
			token: { text: 'File contents', attributes: { done: 'true' } }
		});
		expect(items[3]).toMatchObject({
			token: { text: 'Partial output', attributes: { done: 'false' } }
		});
		expect(getOutputText(output)).toBe('Checking files.\nRunning checks.');
	});

	it('updates a running tool in place without duplicating messages or losing ordering', () => {
		const call = { type: 'function_call', call_id: 'test', name: 'bash', status: 'in_progress' };
		const result = {
			type: 'function_call_output',
			call_id: 'test',
			status: 'in_progress',
			output: [{ text: 'First' }]
		};
		const pending = buildOutputDisplayItems([call, result]);
		const completed = buildOutputDisplayItems([
			{ ...call, status: 'completed' },
			{ ...result, status: 'completed', output: [{ text: 'All checks passed' }] },
			{ type: 'message', id: 'final', content: [{ text: 'Done.' }] }
		]);
		expect(pending[0].id).toBe(completed[0].id);
		expect(completed[0]).toMatchObject({
			token: { text: 'All checks passed', attributes: { done: 'true' } }
		});
		expect(completed.map((item) => item.type)).toEqual(['detail_single', 'message']);
	});
});
