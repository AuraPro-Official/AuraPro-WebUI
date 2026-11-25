import type { TutorialItem, TutorialText } from './tutorials';

const text = (cn: string, en: string, tw = cn): TutorialText => ({
	'zh-CN': cn,
	'zh-TW': tw,
	'en-US': en
});
const none = text('—', '—');
const either = (a: TutorialText, b: TutorialText) =>
	text(
		`${a['zh-CN']} 或 ${b['zh-CN']}`,
		`${a['en-US']} or ${b['en-US']}`,
		`${a['zh-TW']} 或 ${b['zh-TW']}`
	);
const memory = (n: string) => text(`M 芯片 ${n}G`, `M-series ${n}G`, `M 晶片 ${n}G`);
const intel = (n: string) => text(`Intel 内存 ${n}G`, `Intel RAM ${n}G`, `Intel 記憶體 ${n}G`);
const ram = (n: string) => text(`内存 ${n}G`, `RAM ${n}G`, `記憶體 ${n}G`);
const vram = (n: string) => text(`显存 ${n}G`, `VRAM ${n}G`, `顯示記憶體 ${n}G`);
const pc = (r: number, v: number) =>
	text(
		`≥${r}G 内存 + ≥${v}G 显存`,
		`≥${r}G RAM + ≥${v}G VRAM`,
		`≥${r}G 記憶體 + ≥${v}G 顯示記憶體`
	);
const fallback = text('未命中时：≥16G 内存', 'Fallback: ≥16G RAM', '未命中時：≥16G 記憶體');
const lowFallback = text('未命中时：<16G 内存', 'Fallback: <16G RAM', '未命中時：<16G 記憶體');
const headers = [
	text('模型', 'Model'),
	text('模型大小', 'Model size'),
	text('Mac · 质量', 'Mac · Quality', 'Mac · 品質'),
	text('Mac · 均衡', 'Mac · Balanced'),
	text('Windows/Linux · 质量', 'Windows/Linux · Quality', 'Windows/Linux · 品質'),
	text('Windows/Linux · 均衡', 'Windows/Linux · Balanced')
];
const row = (
	name: string,
	size: number,
	macQuality: TutorialText,
	macBalanced: TutorialText,
	pcQuality: TutorialText,
	pcBalanced: TutorialText
) => [
	text(name, name),
	text(`~${size}G`, `~${size}G`),
	macQuality,
	macBalanced,
	pcQuality,
	pcBalanced
];
const high2 = either(ram('≥64'), pc(48, 4));
const macLowest = either(intel('<16'), memory('<8'));
const macSmall = either(intel('≥16'), memory('8–<16'));

export const standardModelTutorial: TutorialItem = {
	id: 'standard-model-requirements',
	title: text('普通模型自动推荐规则', 'Standard model recommendations', '一般模型自動推薦規則'),
	summary: text(
		'安装向导关闭 Pro 时使用。',
		'Used when Pro models are off in setup.',
		'安裝精靈關閉 Pro 時使用。'
	),
	steps: [],
	table: {
		headers,
		rows: [
			row('lowest_V2', 2, macLowest, macLowest, lowFallback, lowFallback),
			row('low_E4', 4.5, none, macSmall, fallback, either(vram('≥6'), fallback)),
			row('medium_Q3_V2', 5, macSmall, none, vram('6–<8'), none),
			row('medium_Q4_V2', 6, memory('16–<24'), memory('16–<24'), vram('≥8'), vram('8–<12')),
			row('medium_IQ2', 11, none, none, pc(24, 4), pc(24, 4)),
			row(
				'high_Q4',
				15,
				memory('24–<32'),
				memory('24–<32'),
				either(ram('≥48'), pc(32, 4)),
				either(vram('≥12'), pc(32, 4))
			),
			row('high_Q4_2', 21, memory('≥32'), memory('≥32'), high2, high2)
		]
	},
	tips: [
		text(
			'Windows/Linux 冲突优先级：high_Q4_2 → medium_Q4_V2 → high_Q4 → medium_Q3_V2 → medium_IQ2 → low_E4 → lowest_V2。',
			'Windows/Linux priority: high_Q4_2 → medium_Q4_V2 → high_Q4 → medium_Q3_V2 → medium_IQ2 → low_E4 → lowest_V2.',
			'Windows/Linux 衝突優先順序：high_Q4_2 → medium_Q4_V2 → high_Q4 → medium_Q3_V2 → medium_IQ2 → low_E4 → lowest_V2。'
		)
	]
};

export const proModelTutorial: TutorialItem = {
	id: 'pro-model-requirements',
	title: text('Pro 模型自动推荐规则', 'Pro model recommendations', 'Pro 模型自動推薦規則'),
	summary: text(
		'安装向导开启 Pro 时，按档位推荐符合配置的模型。',
		'When enabled in setup, Pro recommends a model that meets the selected hardware tier.',
		'安裝精靈開啟 Pro 時，依檔位推薦符合配置的模型。'
	),
	steps: [],
	table: {
		headers,
		rows: [
			row('Q2_0', 66, none, none, pc(48, 12), either(pc(64, 8), pc(48, 12))),
			row('IQ2_XS', 72, none, none, pc(64, 8), pc(64, 12)),
			row('Q2_K_XL', 75, none, none, pc(64, 12), either(pc(64, 16), pc(96, 12))),
			row('IQ3_XXS', 79, none, none, pc(64, 16), pc(96, 16)),
			row('Q3_K_XL', 84, none, memory('96–<128'), pc(96, 12), none),
			row('IQ4_XS', 88, memory('96–<128'), memory('≥128'), pc(96, 16), none),
			row('Q4_K_XL', 104, memory('≥128'), none, none, none)
		]
	},
	tips: [
		text(
			'不符合任何条件时，自动安装模型显示为「无」。表中的空白档不自动推荐；支持 Pro 的平台可以手动选择型号。多个型号同时符合时，优先选择表中靠下的型号。',
			'If no conditions match, setup shows None. A dash means no automatic recommendation for that tier; on supported platforms a model can still be selected manually. If multiple models match, the last matching row wins.',
			'不符合任何條件時，自動安裝模型顯示為「無」。表中的空白檔不自動推薦；支援 Pro 的平台可以手動選擇型號。多個型號同時符合時，優先選擇表中靠下的型號。'
		),
		text(
			'以上为推荐配置，不是实测最低要求。Windows 建议将虚拟内存设为 32G 或以上。长上下文和图片输入需要额外内存；磁盘还需预留运行时、辅助文件及下载临时空间。',
			'These are recommendations, not tested minimums. On Windows, a page file of at least 32G is recommended. Longer contexts and image input need more memory; the runtime, supporting files, and temporary downloads need additional disk space.',
			'以上為建議配置，不是實測最低需求。Windows 建議將虛擬記憶體設為 32G 或以上。長上下文和圖片輸入需要額外記憶體；磁碟還需預留執行環境、輔助檔案及下載暫存空間。'
		),
		text(
			'Windows/Linux 使用 AuraPro Engine；Mac M 芯片使用独立配置的 llama.cpp。',
			'Windows/Linux use AuraPro Engine. M-series Macs use a separately configured llama.cpp.',
			'Windows/Linux 使用 AuraPro Engine；Mac M 晶片使用獨立配置的 llama.cpp。'
		)
	]
};

export const modelHardwareTutorial: TutorialItem = {
	id: 'model-hardware',
	title: text('模型配置要求', 'Model hardware requirements', '模型配置需求'),
	summary: text(
		'普通和 Pro 模型的手动选型参考，以质量挡为主。',
		'Hardware guidance for choosing standard and Pro models manually, based on the Quality tier.',
		'一般和 Pro 模型的手動選型參考，以品質檔為主。'
	),
	steps: [],
	table: {
		headers: [headers[0], headers[1], text('Windows/Linux', 'Windows/Linux'), text('Mac', 'Mac')],
		rows: [
			['lowest_V2', 2, 'RAM 8G', 'UMA 8G'],
			['low_E4', 4.5, 'RAM 16G', 'UMA 8G'],
			['medium_Q3_V2', 5, 'VRAM 6G', 'UMA 8G'],
			['medium_Q4_V2', 6, 'VRAM 8G', 'UMA 16G'],
			['medium_IQ2', 11, 'RAM+VRAM 24G+4G', 'UMA 18G'],
			['high_Q4', 15, 'RAM 48G or RAM+VRAM 32G+4G', 'UMA 24G'],
			['high_Q4_2', 21, 'RAM 64G or RAM+VRAM 48G+4G', 'UMA 32G'],
			['Q2_0', 66, 'RAM+VRAM 48G+12G', 'UMA ~80G'],
			['IQ2_XS', 72, 'RAM+VRAM 64G+8G', 'UMA ~96G'],
			['Q2_K_XL', 75, 'RAM+VRAM 64G+12G', 'UMA ~96G'],
			['IQ3_XXS', 79, 'RAM+VRAM 64G+16G', 'UMA ~96G'],
			['Q3_K_XL', 84, 'RAM+VRAM 96G+12G', 'UMA 96G'],
			['IQ4_XS', 88, 'RAM+VRAM 96G+16G', 'UMA 96G'],
			['Q4_K_XL', 104, 'RAM+VRAM 96G+16G', 'UMA 128G']
		].map(([name, size, windowsLinux, mac]) => [
			text(String(name), String(name)),
			text(`~${size}G`, `~${size}G`),
			text(String(windowsLinux).replaceAll(' or ', ' 或 '), String(windowsLinux)),
			text(String(mac).replace(' or ', ' 或 '), String(mac), String(mac).replace(' or ', ' 或 '))
		])
	},
	tips: [
		text(
			'RAM+VRAM 按顺序表示系统内存和显存，不是两者的总和。UMA 指 Apple M 系列的统一内存。这些数值用于手动选型，不是实测最低要求，也不改变自动推荐条件。',
			'RAM+VRAM lists system memory and video memory separately, in that order. UMA means unified memory on Apple M-series Macs. These are not tested minimums and do not change automatic recommendations.',
			'RAM+VRAM 依序表示系統記憶體和顯示記憶體，不是兩者的總和。UMA 指 Apple M 系列的統一記憶體。這些數值用於手動選型，不是實測最低需求，也不改變自動推薦條件。'
		)
	]
};
