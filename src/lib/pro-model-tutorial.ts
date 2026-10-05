import type { TutorialItem, TutorialText } from './tutorials';

const text = (cn: string, en: string, tw = cn): TutorialText => ({
	'zh-CN': cn,
	'zh-TW': tw,
	'en-US': en
});
const memory = (gb: number) =>
	text(`≥${gb}GB统一内存`, `≥${gb}GB unified memory`, `≥${gb}GB統一記憶體`);
const gpu = (vram: number, ram: number) =>
	text(
		`${vram}GB显存+${ram}GB内存`,
		`${vram}GB VRAM + ${ram}GB RAM`,
		`${vram}GB顯示記憶體+${ram}GB記憶體`
	);
const alternatives = (a: TutorialText, b: TutorialText): TutorialText => ({
	'zh-CN': `${a['zh-CN']} 或 ${b['zh-CN']}`,
	'zh-TW': `${a['zh-TW']} 或 ${b['zh-TW']}`,
	'en-US': `${a['en-US']} or ${b['en-US']}`
});
const q2 = alternatives(gpu(8, 64), gpu(12, 48));
const swift = text(
	'Swift 系列据模型说明可保留约 98% 的原模型质量，并减少约 50% 的思考时间，请自行衡量是否使用。',
	'According to its model description, Swift retains about 98% of the original quality and reduces thinking time by about 50%. Consider the trade-off.',
	'Swift 系列據模型說明可保留約 98% 的原模型品質，並減少約 50% 的思考時間，請自行衡量是否使用。'
);
const none = text('—', '—');
const row = (
	name: string,
	gb: number,
	uma: number,
	hardware: TutorialText,
	note = none
): TutorialText[] => [
	text(name, name),
	text(`约 ${gb}GB`, `~${gb}GB`, `約 ${gb}GB`),
	memory(uma),
	hardware,
	note
];

export const proModelTutorial: TutorialItem = {
	id: 'pro-model-requirements',
	title: text(
		'Pro 模型选择与配置要求',
		'Pro model selection and requirements',
		'Pro 模型選擇與配置需求'
	),
	summary: text(
		'各型号的大小、Mac M 芯片与 Windows/Linux 推荐配置及选择建议。',
		'Model sizes, recommended Mac M-series and Windows/Linux hardware, and selection notes.',
		'各型號的大小、Mac M 晶片與 Windows/Linux 建議配置及選擇建議。'
	),
	steps: [],
	table: {
		headers: [
			text('Pro 模型', 'Pro model'),
			text('模型大小', 'Model size'),
			text('Mac（仅 M 芯片）', 'Mac (M-series only)', 'Mac（僅 M 晶片）'),
			text('Windows/Linux', 'Windows/Linux'),
			text('备注', 'Notes', '備註')
		],
		rows: [
			row(
				'Q2_0',
				66.4,
				80,
				q2,
				text('不作为优先推荐。', 'Not a first-choice recommendation.', '不作為優先推薦。')
			),
			row('IQ2_XS', 68, 80, q2),
			row('IQ3_XXS', 75.8, 96, gpu(12, 64)),
			row('IQ3_S', 83.6, 96, gpu(12, 64)),
			row('Swift IQ2_XS', 68, 80, q2, swift),
			row('Swift IQ3_XXS', 75.8, 96, gpu(12, 64), swift),
			row(
				'Coder IQ1_M',
				58.4,
				80,
				alternatives(gpu(8, 48), gpu(12, 32)),
				text(
					'适合英文编程且配置较低的用户；其他情况优先选择其他模型。',
					'For English coding on lower-spec hardware; prefer other models otherwise.',
					'適合英文程式設計且配置較低的使用者；其他情況優先選擇其他模型。'
				)
			),
			row(
				'Q4',
				111.3,
				128,
				gpu(16, 64),
				text(
					'不作为优先推荐，除非其他模型的质量无法满足需求。',
					'Not a first choice unless other models cannot meet your quality requirements.',
					'不作為優先推薦，除非其他模型的品質無法滿足需求。'
				)
			)
		]
	},
	tips: [
		text(
			'以上为推荐配置，不是实测最低要求。长上下文和图片输入需要额外内存；磁盘还需预留运行库与下载临时文件空间。',
			'These are recommendations, not tested minimums. Longer contexts and image input need additional memory; allow disk space for the runtime and temporary downloads.',
			'以上為建議配置，不是實測最低需求。長上下文和圖片輸入需要額外記憶體；磁碟還需預留執行庫與下載暫存檔案空間。'
		),
		text(
			'Swift 的质量和思考时间数据为参考值，具体效果随任务和运行环境变化。',
			'Swift quality and thinking-time figures are reference values; results vary by task and runtime.',
			'Swift 的品質和思考時間資料為參考值，具體效果隨任務和執行環境變化。'
		),
		text(
			'Windows/Linux 使用 Strata；Mac M 芯片使用独立配置的 llama.cpp。Q4 的 Strata 预设仅支持 CUDA，不支持 HIP。',
			'Windows/Linux use Strata; Mac M-series uses independently configured llama.cpp. The Strata Q4 preset supports CUDA only, not HIP.',
			'Windows/Linux 使用 Strata；Mac M 晶片使用獨立配置的 llama.cpp。Q4 的 Strata 預設僅支援 CUDA，不支援 HIP。'
		)
	]
};
