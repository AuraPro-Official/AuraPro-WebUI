export interface TutorialText {
	'zh-CN': string;
	'zh-TW': string;
	'en-US': string;
}

export interface TutorialLink {
	label: TutorialText;
	url: string;
}

export interface TutorialItem {
	id: string;
	title: TutorialText;
	summary: TutorialText;
	steps: TutorialText[];
	tips?: TutorialText[];
	links?: TutorialLink[];
}

export interface TutorialSection {
	id: string;
	title: TutorialText;
	description: TutorialText;
	items: TutorialItem[];
}

export const tutorialUiText = {
	intro: {
		'zh-CN':
			'这里按安装、更新和常用功能整理了 AuraPro Desktop 与 Open WebUI 教程。两端使用同一套内容，视频链接会直接跳到对应时间线。',
		'zh-TW':
			'這裡按安裝、更新和常用功能整理了 AuraPro Desktop 與 Open WebUI 教程。兩端使用同一套內容，影片連結會直接跳到對應時間線。',
		'en-US':
			'Tutorials for AuraPro Desktop and Open WebUI are grouped by installation, updates, and common features. Both use the same content, and video links jump to the matching timestamp.'
	},
	searchPlaceholder: {
		'zh-CN': '搜索教程、功能或问题',
		'zh-TW': '搜尋教程、功能或問題',
		'en-US': 'Search tutorials, features, or issues'
	},
	stepsLabel: {
		'zh-CN': '要点',
		'zh-TW': '要點',
		'en-US': 'Key points'
	},
	tipsLabel: {
		'zh-CN': '提示',
		'zh-TW': '提示',
		'en-US': 'Tips'
	},
	linksLabel: {
		'zh-CN': '相关视频',
		'zh-TW': '相關影片',
		'en-US': 'Related videos'
	},
	noResults: {
		'zh-CN': '未找到相关教程',
		'zh-TW': '未找到相關教程',
		'en-US': 'No matching tutorials'
	}
} satisfies Record<string, TutorialText>;

const text = (zhCN: string, enUS: string, zhTW = zhCN): TutorialText => ({
	'zh-CN': zhCN,
	'zh-TW': zhTW,
	'en-US': enUS
});

export const getTutorialText = (value: TutorialText, language?: string): string => {
	const normalized = (language ?? '').toLowerCase();
	if (normalized.startsWith('zh-tw') || normalized.startsWith('zh-hk')) {
		return value['zh-TW'] || value['zh-CN'] || value['en-US'];
	}
	if (normalized.startsWith('zh')) {
		return value['zh-CN'] || value['zh-TW'] || value['en-US'];
	}
	return value['en-US'] || value['zh-CN'] || value['zh-TW'];
};

const mainVideo = (seconds: number) =>
	`https://www.youtube.com/watch?v=vDgVZdkTy-c&list=PLZ97w0vYgxC4&t=${seconds}s`;
const video = (zhCN: string, enUS: string, url: string, zhTW = zhCN): TutorialLink => ({
	label: text(zhCN, enUS, zhTW),
	url
});

export const tutorialSections: TutorialSection[] = [
	{
		id: 'install-update',
		title: text('软件安装/更新', 'Install and updates'),
		description: text(
			'安装流程、模块作用、llama.cpp 选择、中文路径、空间、重置和更新。',
			'Install flow, module roles, llama.cpp choice, paths, disk space, reset, and updates.'
		),
		items: [
			{
				id: 'install-flow-modules',
				title: text('安装流程和各模块作用', 'Install flow and module roles'),
				summary: text(
					'了解安装向导里每个模块的用途，避免安装不需要的组件。',
					'Understand what each installer module does so users only install what they need.'
				),
				steps: [
					text(
						'Open WebUI 是本地 WebUI 服务，负责聊天界面、词典、知识库、设置和用户数据。',
						'Open WebUI is the local web service for chat, dictionaries, knowledge bases, settings, and user data.'
					),
					text(
						'llama.cpp 是本地模型推理运行时。需要本地模型时安装；只连接远程服务器时可以不依赖它。',
						'llama.cpp is the local model runtime. Install it for local models; remote-only users do not need it.'
					),
					text(
						'Open Terminal 是可选工具，用于让模型访问终端能力。普通翻译和聊天用户可以不安装。',
						'Open Terminal is optional and lets models use terminal capabilities. Normal translation/chat users can skip it.'
					),
					text(
						'sherpa 是可选语音服务，用于语音输入、语音输出和语音翻译，首次启动会下载语音模型。',
						'sherpa is optional speech service for voice input, voice output, and speech translation. First start downloads speech models.'
					)
				],
				links: [
					video('安装和下载 0:30', 'Install and download 0:30', mainVideo(30)),
					video('模型选择 2:15', 'Model selection 2:15', mainVideo(135)),
					video('可选功能 4:35', 'Optional features 4:35', mainVideo(275))
				]
			},
			{
				id: 'llama-variant-choice',
				title: text('llama.cpp 运行变体怎么选', 'How to choose a llama.cpp variant'),
				summary: text(
					'根据系统和显卡选择 CPU、Metal、CUDA 12.4、CUDA 13.3、Vulkan 等变体。',
					'Choose CPU, Metal, CUDA 12.4, CUDA 13.3, Vulkan, and other variants based on OS and GPU.'
				),
				steps: [
					text(
						'macOS 默认使用 Metal；没有独立显卡或不确定时选择 CPU/默认即可。',
						'On macOS, use Metal by default. Without a dedicated GPU, or if unsure, choose CPU/default.'
					),
					text(
						'Windows 有 NVIDIA 显卡时优先 CUDA。RTX 40 系及更早默认 CUDA 12.4，RTX 50 系默认 CUDA 13.3。',
						'On Windows with NVIDIA, prefer CUDA. RTX 40-series and older default to CUDA 12.4; RTX 50-series defaults to CUDA 13.3.'
					),
					text(
						'AMD/Intel 独显可尝试 Vulkan。诊断建议切换变体时，修复会同步修改推理运行时设置。',
						'AMD/Intel discrete GPUs can try Vulkan. When diagnostics suggests a variant switch, repair also updates inference runtime settings.'
					),
					text(
						'CUDA 变体会自动安装 llama.cpp 官方运行库，不需要用户安装 CUDA 开发套件。',
						'CUDA variants install official llama.cpp runtime DLLs automatically; users do not need the CUDA toolkit.'
					)
				],
				links: [
					video('安装·模型 2:15', 'Install: models 2:15', mainVideo(135)),
					video('安装·常见问题 5:30', 'Install: notes and common issues 5:30', mainVideo(330))
				]
			},
			{
				id: 'path-security',
				title: text('中文路径和安全设置', 'Chinese paths and security settings'),
				summary: text(
					'安装目录、用户名、杀毒软件或系统安全设置可能影响本地组件启动。',
					'Install path, username, antivirus, or OS security settings may affect local components.'
				),
				steps: [
					text(
						'Windows 用户名或安装路径包含中文、乱码或特殊字符时，sherpa 等组件可能无法读取文件。',
						'If the Windows username or install path contains Chinese, garbled text, or special characters, components like sherpa may fail to read files.'
					),
					text(
						'推荐使用英文路径，例如 D:\\AuraPro。路径只包含英文、数字、空格、短横线和下划线最稳。',
						'Use an English path such as D:\\AuraPro. Paths with letters, numbers, spaces, hyphens, and underscores are safest.'
					),
					text(
						'部分国家或环境下，macOS Gatekeeper、Windows Defender、Bitdefender 等安全设置可能需要手动允许。',
						'In some regions or environments, macOS Gatekeeper, Windows Defender, Bitdefender, or similar tools may require manual approval.'
					)
				],
				links: [
					video('安装注意事项/常见问题 5:30', 'Install notes/common issues 5:30', mainVideo(330)),
					video(
						'MAC 安全设置（Windows 也适用）',
						'Mac security settings, also useful for Windows',
						'https://www.youtube.com/watch?v=kAPD9KCdIwU&list=PLZ97w0vYgxC4&index=2'
					),
					video(
						'Windows 安全设置·方法 2',
						'Windows security settings method 2',
						'https://www.youtube.com/watch?v=YK4wr7sPwFg&list=PLZ97w0vYgxC4&index=3'
					)
				]
			},
			{
				id: 'disk-space',
				title: text('硬盘空间和模型下载', 'Disk space and model downloads'),
				summary: text(
					'模型、语音模型、llama.cpp 和可选 GPU PyTorch 都会占用空间。',
					'Models, speech models, llama.cpp, and optional GPU PyTorch all consume disk space.'
				),
				steps: [
					text(
						'安装向导会显示预计所需空间和当前磁盘剩余空间。空间不足时先清理磁盘或更换安装位置。',
						'The installer shows required and available disk space. Free disk space or change install location if needed.'
					),
					text(
						'本地模型越大，下载时间和磁盘占用越高。首次模型下载失败时可以重新下载。',
						'Larger local models need more download time and disk space. If first download fails, re-download the model.'
					),
					text(
						'默认安装 CPU 版 PyTorch。只有选择 CUDA 变体并开启 RAG 硬件加速时，才安装 GPU 版 PyTorch。',
						'CPU PyTorch is installed by default. GPU PyTorch is installed only when CUDA is selected and RAG acceleration is enabled.'
					)
				],
				links: [
					video('安装·模型 2:15', 'Install: models 2:15', mainVideo(135)),
					video('下载额外模型 27:15', 'Download extra models 27:15', mainVideo(1635))
				]
			},
			{
				id: 'factory-reset',
				title: text('恢复出厂设置和重新安装', 'Factory reset and reinstall'),
				summary: text(
					'适用于安装中断、恢复出厂后残留模块、无法重新进入向导等情况。',
					'For interrupted installs, leftover modules after reset, or being unable to re-enter setup.'
				),
				steps: [
					text(
						'恢复出厂设置会停止本地服务，并清理 Python、包、模型、连接和相关数据。',
						'Factory reset stops local services and removes Python, packages, models, connections, and related data.'
					),
					text(
						'如果安装一半关闭软件，重新打开后应继续安装或重新进入向导；如果卡住，先运行诊断。',
						'If the app closes mid-install, reopen it to continue or re-enter setup. If stuck, run diagnostics first.'
					),
					text(
						'恢复后仍看到残留模块时，检查安装路径权限和残留文件，再重新安装。',
						'If modules remain after reset, check install path permissions and leftover files before reinstalling.'
					)
				],
				links: [video('如何重置设置 12:33', 'How to reset settings 12:33', mainVideo(753))]
			},
			{
				id: 'updates-changelog',
				title: text('软件更新和查看更新日志', 'Updates and changelog'),
				summary: text(
					'了解桌面端、WebUI、本地运行库的更新入口和更新日志位置。',
					'Learn where to update desktop, WebUI, local runtimes, and where to view changelogs.'
				),
				steps: [
					text(
						'桌面端更新在设置的关于页面检查；macOS 使用半自动 DMG 更新流程。',
						'Desktop updates are checked in Settings > About. macOS uses a semi-automatic DMG flow.'
					),
					text(
						'WebUI 默认跟随桌面端版本，调试时可在桌面端 AuraPro 设置里手动填写版本。',
						'WebUI defaults to the desktop-matched version. For testing, set a custom version in desktop AuraPro settings.'
					),
					text(
						'更新日志可在关于页面查看，也可以通过版本发布页查看完整记录。',
						'View changelog in the About page or open the release page for full notes.'
					)
				],
				links: [video('如何更新 11:15', 'How to update 11:15', mainVideo(675))]
			}
		]
	},
	{
		id: 'features',
		title: text('核心功能介绍', 'Core features'),
		description: text(
			'聊天、词典、翻译模式、语音、多模态、记忆、多开和共享。',
			'Chat, dictionaries, translation mode, voice, multimodal, memory, multiple instances, and sharing.'
		),
		items: [
			{
				id: 'basic-usage-shortcuts',
				title: text('基础使用和快捷键', 'Basic usage and shortcuts'),
				summary: text(
					'从聊天、模型选择、发送消息、快捷键开始了解日常使用。',
					'Start with chat, model selection, sending messages, and shortcuts.'
				),
				steps: [
					text(
						'打开本地 Open WebUI 后，可以直接聊天、选择模型、上传文件或使用扩展模式。',
						'After opening local Open WebUI, users can chat, select models, upload files, or use extension modes.'
					),
					text(
						'快捷键适合快速呼出软件，也可以绑定翻译模式、同传模式等附加功能。',
						'Shortcuts can bring up the app quickly and can bind extra actions such as translation or interpretation mode.'
					),
					text(
						'遇到无法发送、卡住或模型无响应时，先查看右下角诊断和日志。',
						'If sending fails, the app hangs, or models do not respond, check bottom-right diagnostics and logs first.'
					)
				],
				links: [
					video('使用·基础功能 13:23', 'Usage: basics 13:23', mainVideo(803)),
					video('快捷键介绍 32:50', 'Shortcut guide 32:50', mainVideo(1970))
				]
			},
			{
				id: 'dictionary',
				title: text('词典和导入导出', 'Dictionaries and import/export'),
				summary: text(
					'词典用于翻译模式和文稿翻译，可以维护固定术语、语种和上下文设置。',
					'Dictionaries are used by translation modes to manage terms, language pairs, and context settings.'
				),
				steps: [
					text(
						'词典包含源语言、目标语言、术语和翻译偏好。翻译类用户建议先配置词典。',
						'A dictionary includes source language, target language, terms, and translation preferences. Translation users should configure dictionaries first.'
					),
					text(
						'官方词典不再随软件安装。请打开桌面端“设置 → 官方词典”，输入内部提供的下载密码进行安装或更新。',
						'Official glossaries are no longer bundled with the app. Open Desktop Settings → Official Glossaries and enter the internally provided password to install or update them.'
					),
					text(
						'导入导出用于备份、迁移或分享词典。导入后检查语言方向和术语是否正确。',
						'Import/export is used for backup, migration, or sharing. After import, verify language direction and terms.'
					),
					text(
						'设置向导里选择翻译/翻译文稿时，也会引导选择或创建词典。',
						'Setup Wizard also guides users to select or create dictionaries when Translation or Manuscript Translation is selected.'
					)
				],
				links: [video('导入导出词典 23:20', 'Import/export dictionaries 23:20', mainVideo(1400))]
			},
			{
				id: 'translation-mode',
				title: text('翻译模式和默认扩展模式', 'Translation mode and default extension mode'),
				summary: text(
					'适用于翻译、翻译文稿、同传等高频翻译工作流。',
					'For translation, manuscript translation, interpretation, and frequent translation workflows.'
				),
				steps: [
					text(
						'翻译模式适合短文本和日常翻译；翻译文稿适合长文档；同传适合语音场景。',
						'Translation mode fits short text and daily translation; manuscript translation fits long documents; interpretation fits voice scenarios.'
					),
					text(
						'可以把常用扩展模式设为默认，避免每次启动后手动开启。',
						'Set a frequent extension mode as default so it is enabled automatically after startup.'
					),
					text(
						'翻译和同传场景默认不使用上下文精简，避免影响原文完整性。',
						'Translation and interpretation disable context compaction by default to preserve source text integrity.'
					)
				],
				links: [
					video('使用·基础功能 13:23', 'Usage: basics 13:23', mainVideo(803)),
					video(
						'语音输入/输出（语音翻译）24:00',
						'Voice input/output and speech translation 24:00',
						mainVideo(1440)
					)
				]
			},
			{
				id: 'voice-translation',
				title: text('语音输入/输出和语音翻译', 'Voice input/output and speech translation'),
				summary: text(
					'了解 sherpa、多模态语音识别、语音输入输出和语音翻译。',
					'Learn sherpa, multimodal speech recognition, voice input/output, and speech translation.'
				),
				steps: [
					text(
						'sherpa 负责本地语音识别和语音合成；首次启动会下载模型，可能需要 5-15 分钟。',
						'sherpa handles local speech recognition and synthesis. First start downloads models and may take 5-15 minutes.'
					),
					text(
						'多模态语音识别适合部分模型能力更强的场景，但会依赖当前模型和运行时支持。',
						'Multimodal speech recognition can be better for some models but depends on model and runtime support.'
					),
					text(
						'如果语音模型加载失败，运行诊断检查模型完整性、路径和日志。',
						'If speech models fail to load, run diagnostics to check model integrity, paths, and logs.'
					)
				],
				links: [
					video(
						'语音输入/输出（语音翻译）24:00',
						'Voice input/output and speech translation 24:00',
						mainVideo(1440)
					),
					video(
						'多模态语音识别（语音翻译）',
						'Multimodal speech recognition',
						'https://www.youtube.com/watch?v=GSXhuJBsAMQ&list=PLZ97w0vYgxC4&index=5'
					)
				]
			},
			{
				id: 'multimodal-memory',
				title: text('多模态、记忆和图片发送', 'Multimodal, memory, and image input'),
				summary: text(
					'适用于发送图片、多模态模型、记忆自动写入和个性化回答。',
					'For image input, multimodal models, memory writing, and personalized responses.'
				),
				steps: [
					text(
						'多模态用于发送图片等内容，只有首次使用或运行失败时才需要检查多模态模型是否加载。',
						'Multimodal is for images and similar inputs. Multimodal model checks are needed on first use or when a multimodal run fails.'
					),
					text(
						'记忆功能可以让软件记录用户偏好；自动写入记忆默认关闭，可由管理员开启。',
						'Memory stores user preferences. Background memory writing is off by default and can be enabled by admins.'
					),
					text(
						'如果图片无法识别，检查基础模型、mmproj 文件和当前模型是否支持多模态。',
						'If image recognition fails, check base model, mmproj file, and whether the current model supports multimodal input.'
					)
				],
				links: [
					video('多模态（发送图片等）27:50', 'Multimodal image input 27:50', mainVideo(1670)),
					video('记忆功能 28:35', 'Memory feature 28:35', mainVideo(1715))
				]
			},
			{
				id: 'multi-instance-sharing-feedback',
				title: text('多开、共享和问题反馈', 'Multiple instances, sharing, and feedback'),
				summary: text(
					'了解如何多开、局域网共享给他人使用，以及如何提交问题反馈。',
					'Learn how to run multiple instances, share on LAN, and submit feedback.'
				),
				steps: [
					text(
						'多开适合同时连接不同服务或分离不同工作流，但会占用更多内存和端口。',
						'Multiple instances can connect to different services or separate workflows, but use more memory and ports.'
					),
					text(
						'局域网共享可以让同一网络内的其他设备访问本机 Open WebUI，注意网络和权限安全。',
						'LAN sharing lets other devices on the same network access local Open WebUI. Pay attention to network and permission safety.'
					),
					text(
						'提交问题反馈时尽量附带版本、系统、截图、日志和复现步骤。',
						'When submitting feedback, include version, OS, screenshots, logs, and reproduction steps.'
					)
				],
				links: [
					video('如何多开 29:15', 'How to run multiple instances 29:15', mainVideo(1755)),
					video('共享给他人使用 31:08', 'Share with others 31:08', mainVideo(1868)),
					video('问题反馈 32:25', 'Feedback 32:25', mainVideo(1945))
				]
			}
		]
	},
	{
		id: 'models-knowledge',
		title: text('模型、性能和知识库', 'Models, performance, and knowledge'),
		description: text(
			'额外模型、MTP、上下文大小、知识库、RAG 和常见性能问题。',
			'Extra models, MTP, context size, knowledge bases, RAG, and performance issues.'
		),
		items: [
			{
				id: 'extra-models',
				title: text('下载和增加额外模型', 'Download and add extra models'),
				summary: text(
					'适用于想安装更多本地模型、手动放入 GGUF 或切换模型的用户。',
					'For users who want more local models, manual GGUF placement, or model switching.'
				),
				steps: [
					text(
						'可在模型页面下载额外模型，也可以把 GGUF 文件放入模型目录。',
						'Download extra models from the Models page or place GGUF files in the models directory.'
					),
					text(
						'不同模型需要不同内存和显存。下载前先确认硬件和磁盘空间。',
						'Different models need different RAM/VRAM. Check hardware and disk space before downloading.'
					),
					text(
						'多模态模型通常还需要 mmproj 文件；MTP 还需要对应 draft 模型。',
						'Multimodal models usually need an mmproj file; MTP also needs the matching draft model.'
					)
				],
				links: [
					video('下载额外模型 27:15', 'Download extra models 27:15', mainVideo(1635)),
					video(
						'进阶·增加其他额外模型',
						'Advanced: add extra models',
						'https://www.youtube.com/watch?v=XjbHeMFQeZg&list=PLZ97w0vYgxC4&index=7'
					)
				]
			},
			{
				id: 'mtp',
				title: text('MTP（多 Token 预测）', 'MTP multi-token prediction'),
				summary: text(
					'MTP 可以提升部分模型推理速度，但需要模型和 llama.cpp 版本支持。',
					'MTP can speed up some models but requires model and llama.cpp support.'
				),
				steps: [
					text(
						'MTP 适合支持的中高模型，lowest 和 low 模型默认不使用 MTP。',
						'MTP is for supported medium/high models. Lowest and low models do not use MTP by default.'
					),
					text(
						'首次加载 MTP draft 模型会比较慢，不能过早判断为失败。',
						'The first MTP draft model load can be slow, so do not mark it failed too early.'
					),
					text(
						'如果 MTP 报错，检查 llama.cpp 版本、draft 模型是否完整、当前模型是否支持。',
						'If MTP errors occur, check llama.cpp version, draft model completeness, and whether the current model supports it.'
					)
				],
				links: [
					video(
						'MTP（多 Token 预测）',
						'MTP multi-token prediction',
						'https://www.youtube.com/watch?v=7dJK2tEF5a0&list=PLZ97w0vYgxC4&index=6'
					)
				]
			},
			{
				id: 'context-size',
				title: text('上下文大小和上下文精简', 'Context size and context compaction'),
				summary: text(
					'上下文大小影响能发送的文本长度，也会影响内存占用。',
					'Context size affects how much text can be sent and how much memory is used.'
				),
				steps: [
					text(
						'默认上下文大小适合普通用户。太小会导致长消息无法发送；太大占用更多内存。',
						'The default context size fits most users. Too small blocks long messages; too large uses more memory.'
					),
					text(
						'词典设置、设置向导和桌面端 llama.cpp 设置应保持同步。修改后通常需要重启本地运行时。',
						'Dictionary settings, Setup Wizard, and desktop llama.cpp settings should stay synced. Changes usually require runtime restart.'
					),
					text(
						'普通聊天可开启上下文精简；翻译模式和同传模式默认使用硬截断以保持翻译输入稳定。',
						'Normal chat can use context compaction. Translation and interpretation default to hard truncation for stable translation input.'
					)
				]
			},
			{
				id: 'knowledge-rag',
				title: text('知识库和 RAG', 'Knowledge bases and RAG'),
				summary: text(
					'知识库让模型基于导入资料回答；RAG 硬件加速用于提升检索/向量相关性能。',
					'Knowledge bases let models answer from imported documents. RAG acceleration improves retrieval/vector-related performance.'
				),
				steps: [
					text(
						'在 WebUI 知识库里创建知识库并上传资料，等待解析和索引完成。',
						'Create a knowledge base in WebUI, upload documents, and wait for parsing/indexing to finish.'
					),
					text(
						'聊天时选择对应知识库。如果检索不到内容，检查文档是否解析成功、知识库是否选中、问题关键词是否足够。',
						'Select the knowledge base in chat. If retrieval misses content, check parsing status, selected knowledge base, and question keywords.'
					),
					text(
						'RAG 硬件加速默认关闭。只有 CUDA 用户明确开启时才安装 GPU 版 PyTorch。',
						'RAG acceleration is off by default. GPU PyTorch is installed only when CUDA users explicitly enable it.'
					)
				]
			},
			{
				id: 'common-runtime-issues',
				title: text('运行异常和性能问题', 'Runtime and performance issues'),
				summary: text(
					'适用于 GPU 未使用、驱动过低、内存不足、模型加载失败或卡住。',
					'For GPU not being used, old drivers, low memory, model load failures, or hangs.'
				),
				steps: [
					text(
						'如果 CUDA 变体没有使用显卡，诊断会检查 NVIDIA 显卡、驱动、CUDA DLL、显存占用和 llama.cpp 日志。',
						'If CUDA does not use the GPU, diagnostics checks NVIDIA GPU, driver, CUDA DLLs, VRAM usage, and llama.cpp logs.'
					),
					text(
						'模型加载失败时，诊断会区分基础模型、MTP、多模态模型、内存不足等原因。',
						'For model load failures, diagnostics distinguishes base model, MTP, multimodal model, and memory issues.'
					),
					text(
						'如果软件没有明显异常且 llama.cpp 已检测到 GPU 并占用显存，不应再提示未使用 GPU。',
						'If the app is healthy and llama.cpp detects the GPU with VRAM usage, it should not report GPU acceleration missing.'
					)
				],
				links: [video('使用·常见问题 20:40', 'Usage common issues 20:40', mainVideo(1240))]
			}
		]
	}
];
