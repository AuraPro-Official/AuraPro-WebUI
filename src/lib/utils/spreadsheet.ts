export type SpreadsheetCell = string | number | boolean | Date | null | undefined;

export interface SpreadsheetWorkbook {
	SheetNames: string[];
	Sheets: Record<string, SpreadsheetCell[][]>;
}

const MAX_SPREADSHEET_BYTES = 100 * 1024 * 1024;
const MAX_SHEETS = 64;
const MAX_ROWS_PER_SHEET = 5000;
const MAX_COLUMNS_PER_ROW = 200;

const limitRows = (rows: SpreadsheetCell[][]): SpreadsheetCell[][] =>
	rows
		.slice(0, MAX_ROWS_PER_SHEET)
		.map((row) => row.slice(0, MAX_COLUMNS_PER_ROW) as SpreadsheetCell[]);

export const parseSpreadsheet = async (
	arrayBuffer: ArrayBuffer,
	filename = 'spreadsheet.xlsx'
): Promise<SpreadsheetWorkbook> => {
	if (arrayBuffer.byteLength > MAX_SPREADSHEET_BYTES) {
		throw new Error('Spreadsheet preview is limited to 100 MB.');
	}

	const extension = filename.split('.').pop()?.toLowerCase();
	if (extension === 'csv' || extension === 'tsv') {
		const Papa = (await import('papaparse')).default;
		const result = Papa.parse<SpreadsheetCell[]>(new TextDecoder().decode(arrayBuffer), {
			delimiter: extension === 'tsv' ? '\t' : '',
			dynamicTyping: true,
			skipEmptyLines: false
		});
		if (result.errors.some((error) => error.type === 'Quotes')) {
			throw new Error('The delimited text file is malformed.');
		}
		return {
			SheetNames: ['Sheet1'],
			Sheets: { Sheet1: limitRows(result.data) }
		};
	}

	const { default: readXlsxFile } = await import('read-excel-file/browser');
	const blob = new Blob([arrayBuffer], {
		type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
	});
	const workbook = (await readXlsxFile(blob)).slice(0, MAX_SHEETS);
	const sheetNames = workbook.map(({ sheet }) => sheet);
	const sheets: Record<string, SpreadsheetCell[][]> = {};

	for (const { sheet, data } of workbook) {
		sheets[sheet] = limitRows(data as unknown as SpreadsheetCell[][]);
	}

	return { SheetNames: sheetNames, Sheets: sheets };
};
