# Changelog

**English** | [Simplified Chinese](CHANGELOG.zh-CN.md)

All notable changes to AuraPro Desktop are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.6.5] - 2026-06-19

### Added

- Added Document Translation mode. Terms matched by the active glossary are displayed in bold for easier proofreading.
- Added restart buttons to service log views, allowing users to quickly restart the corresponding module when troubleshooting.
- Added an experimental index mode for translation and document research. Full release notes will be provided with the official v3.7.0 release.

### Changed

- Improved model download validation to detect incomplete downloads more reliably and prevent models interrupted during download from failing to load.
- Improved the appearance of the automatic-update dialog and release notes.

### Fixed

- Fixed an issue where the Bitdefender setup guide did not appear as expected.

## [3.6.0] - 2026-06-15

### Added

- Added MTP (multi-token prediction) support for medium- and high-tier models. Time to first token is approximately twice as fast, while generation speed improves by roughly 1.2x to 2.5x depending on hardware. This feature can be toggled at any time under Settings - Inference Runtime. It is disabled by default for low-tier models and enabled by default for medium- and high-tier models.

### Changed

- Updated CUDA to 13.3 for compatibility with new llama.cpp features.
- Improved software attribution and licensing information.
- Important notice: Some interfaces in earlier releases may present licensing concerns. All users must update to the latest version. Downloads are no longer provided for V3 releases 3.0.0 through 3.5.x, including 3.6.0 and earlier. V1 and V2 releases remain available.

### Fixed

- Fixed an issue that prevented llama.cpp from updating on NVIDIA GPUs and kept it pinned to build b9208.
- Fixed an installation loop in which models were repeatedly downloaded while the initial setup waited for CUDA installation.
- Fixed an intermittent extra console window appearing when AuraPro started on Windows.

## [3.5.1] - 2026-06-14

### Fixed

- Fixed an issue that prevented some users from sending messages and incorrectly reported that the context was too long.

## [3.5.0] - 2026-06-12

### Added

- Added glossary presets for Chinese or English paired with Hindi, Nepali, and Tagalog.
- Added multimodal voice input, allowing voice recordings to be sent directly to the AI as an alternative speech-recognition method. This mode currently supports only the `lowest`, `low`, and `medium_Q4` models.
- Added the `medium_Q4` model, designed for Macs with 16 GB of unified memory and GPUs with 8 GB of VRAM.

### Changed

- Enabled active extension-mode states to persist when switching views.
- Allowed users to begin the next recording before the AI finishes responding.
- Improved performance when context becomes long in both extension modes and standard chat.
- Added automatic context-memory cleanup so conversations can continue after reaching the context limit.
- Removed the `high_F4` and `low_E6_MAC_16G` models.
- Fine-tuned all models to improve accuracy or performance. For best results, delete and re-download older model files.
- Improved automatic model recommendations and corrected inaccurate selections for some hardware configurations.
- Added dedicated installation handling for GTX graphics cards.

## [3.4.7] - 2026-05-29

### Changed

- Expanded voice input support to more languages.

### Fixed

- Fixed incorrect language detection that could make voice input unusable.
- Fixed cases where simultaneous voice translation did not work.
- Replaced the Chinese speech-to-text model to prevent repeated transcription text in long recordings.
## [3.4.0] - 2026-05-28

### Added

- Added a Bitdefender troubleshooting guide. When Bitdefender is detected, an illustrated PDF guide is shown before the initial installation.
- Added mute controls to voice mode, including a mute shortcut and automatic audio restoration after assistant playback finishes.
- Added a Delete Current Chat action with confirmation to the chat menu.
- Added a quick Scroll to Top action to the chat menu for long conversations.
- Added a dedicated event-creation dialog and a quick-create entry in the calendar sidebar.
- Added a coordination API that can unload active models from the model selector and display model loading status.
- Added a Controls panel to Playground for adjusting temperature and other parameters per run.
- Added configuration for the audio file extensions accepted by speech-to-text, enabling support for more formats.
- Voice calls now remember the last selected camera and restore it when available.
- System prompts and prompt templates now support the `{{USER_GROUPS}}` user-group variable.
- Disabled proxying of external avatar URLs to reduce browser metadata exposure.
- Custom connection and tool-server headers now support chat-, message-, and user-related variables.
- Static OAuth tool servers can now configure a separate OAuth Server URL.
- Open Terminal file browsing now supports sorting by name and date, listing directories first, and displaying modification times.
- Added an API for controlling custom prompt behavior in voice mode.
- Rendered LaTeX formulas can now be clicked to copy their source.
- Models can now use dedicated profile images.
- Rich UI embeds from Pipes and Tools can now be replaced in place, allowing progress panels and dashboards to update dynamically.
- Assistant responses can now be edited and continued. Reasoning, tool calls, and text can all be edited before generation resumes.
- Outbound HTTP requests no longer follow 3xx redirects by default, reducing redirect-based SSRF risks.
- Added the `IFRAME_CSP` setting to control the Content Security Policy for srcdoc iframes used by Artifacts, tool embeds, file previews, and citation dialogs.
- Users can disable Markdown rendering separately for user messages and assistant responses.
- Added `TERMINAL_PROXY_HEADERS` for injecting custom response headers into terminal proxy responses.
- Model mentions in Channels now support real-time streaming and the complete chat-completion pipeline, including function calling, built-in tools, user tools, MCP tools, filters, and RAG knowledge injection.

### Changed

- Expanded voice input support to more languages.
- Improved installation logs with installation progress, download progress, download speed, and estimated time remaining.
- Added resumable downloads. Retrying a failed download resumes from the previous progress, and selected failures during initial setup are retried automatically.
- Initial setup now calculates required disk space from the selected model. If space is insufficient, installation pauses and displays the required space, available space, and suggested actions.
- Optimized glossary invocation and prompt construction to improve translation response speed.
- Improved Prompt list and Prompt tab loading, especially for non-admin users with large Prompt libraries.
- Accelerated chat-history loading by preferring normalized message records for large conversations.
- Improved health-check and readiness-check response times and reduced false failures under database pressure.
- Improved user-memory query and deletion performance by adding indexes.
- Improved function dependency installation by skipping packages that are already installed and unchanged.
- Reduced typing latency in the rich-text editor.
- Added multiple general performance, stability, and security improvements.
- Updated Chinese, Catalan, Filipino, Korean, and Brazilian Portuguese localizations.
- Removed the unauthenticated retrieval status endpoint. Related configuration is now available only through authenticated administrator endpoints.
- Updated the pull-request template to require a related Issue or Discussion for better contribution tracking.

### Fixed

- Fixed background code execution hanging indefinitely after switching chats or browser tabs.
- Fixed recording MIME compatibility so more browsers can start voice recording.
- Fixed title, tag, follow-up, emoji, and query task generation for directly connected models.
- Fixed external tools not being ready when a new chat was submitted before the default model value had been applied.
- Fixed MCP tool calls returning an incorrect 500 error during cleanup after a successful invocation.
- Fixed the chat input being locked by unrelated background tasks.
- Fixed pinned Notes affecting every user; pin state is now stored per user.
- Fixed non-string custom header values causing request failures.
- Fixed chats remaining permanently in a loading state after regeneration failed.
- Fixed incomplete messages in downloaded chat screenshots.
- Fixed SQLite write-lock contention when deleting calendar entries.
- Fixed filters overwriting internal tools when adding native tools.
- Fixed OpenAPI tool compatibility with empty paths, non-operation paths, path parameters, and composed schemas.
- Fixed the final frame of streaming Markdown not rendering promptly.
- Fixed Channel webhook avatar URLs not being validated before saving.
- Fixed the page jumping to the top while editing large system prompts.
- Fixed knowledge-base search matching only titles instead of document content.
- Fixed slow Prompt-tag loading.
- Fixed cramped multi-digit citation-count badges.
- Fixed Yandex searches failing entirely when a result omitted optional fields.
- Fixed empty or failed audio conversions still being sent to speech-to-text.
- Fixed excessive concurrent chunked STT requests causing local transcription failures.
- Fixed imported ChatGPT conversations missing model, timestamp, and analysis data.
- Fixed selected knowledge collections disappearing from the chat input after refresh or view changes.
- Fixed leading or trailing spaces in embedding model names causing silent failures.
- Fixed PCM TTS audio playback by converting output to MP3.
- Fixed MCP OAuth token exchange ignoring configured timeout values.
- Fixed PDF previews lacking a text layer, which prevented browser search and text selection.
- Fixed Android password-manager autofill.
- Fixed speech-to-text blocking the service event loop.
- Fixed SearXNG multilingual parameters failing because of extra separators.
- Fixed filenames in the file-details dialog opening the wrong content.
- Fixed files attached by chat tools not appearing in assistant responses.
- Fixed embeds expanding incorrectly in Channel reply previews.
- Hardened external image URL handling to prevent untrusted image URLs from triggering client requests.
- Fixed an HTML sanitization issue in spreadsheet previews.
- Fixed multiple workers continuing to use stale tool code after an update.
- Fixed parsing and application of the `DEFAULT_MODEL_METADATA` environment variable.
- Fixed imported configuration not synchronizing with Redis, which could prevent settings from taking effect.
- Fixed DDGS automatic backend selection, empty-result handling, and rate-limit response compatibility.
- Fixed failures when updating automation tasks from chats.
- Fixed authorization checks for updating and deleting calendar events.
- Fixed cache files outside image, audio, and video types being rendered inline by browsers.
- Fixed inaccurate token statistics for streaming chats.
- Fixed administrators receiving a 401 when opening or cloning shared chat links.
- Fixed chat settings such as the system prompt disappearing after a new chat was created and refreshed.
- Fixed chat controls not saving automatically; system prompts, parameters, attachments, and related settings now persist after refresh.
- Fixed the OneDrive option appearing when no client ID was configured.
- Fixed reasoning content leaking as `<think>` during tool-call turns.
- Fixed the terminal sidebar opening automatically when Open Terminal was disabled.
- Fixed duplicate confirmation dialogs when deleting a connection.
- Fixed background-task cleanup timing that could leave the Stop button or sidebar activity indicator stuck.
- Fixed chats not reliably scrolling to the bottom when opened.
- Fixed a TypeError related to the `is_pinned` parameter when creating or opening Notes.
- Fixed public-sharing permissions not being enforced for Skills.
- Fixed public-sharing permissions not being enforced for Calendar.
- Fixed a user ID spoofing vulnerability.
- Fixed SSRF through redirects in chat image URLs.
- Fixed file-processing endpoints not verifying collection write permission, which could allow content to be injected into another user's knowledge base.
- Tightened permission requirements for updating tool source code to `workspace.tools` or `workspace.tools_import`.
- Fixed Channel message update and deletion operations not verifying message ownership.
- Fixed read-only users being able to pin and unpin Channel messages.
- Fixed image-generation URLs not being validated consistently before retrieval.
- Fixed read-only users being able to view model parameters, system prompts, and other configuration through model APIs.
- Fixed SSRF bypasses caused by differences between URL parsers.
- Restricted avatar data URI MIME types and added `X-Content-Type-Options: nosniff`.
- Fixed folder and knowledge-base attachments not validating read access for every file.
- Fixed authorization for chat owners and administrators opening or cloning their own shared chats.
- Fixed loading of legacy chat histories with damaged parent-child links by repairing and backfilling the normalized message table automatically.
- Fixed model-filter checkbox state not updating reactively with the active filter and current selection.

### Known Issues

- Simultaneous voice translation may not work in some situations.
- Voice-input language recognition currently supports only Chinese and English.
## [3.3.8] - 2026-05-21

### Added

- Added adaptive TTS, which automatically detects the language of the input text and selects the corresponding TTS model.
- Added multimodal file support for images, videos, documents, and other visual content. A compatible vision projector is downloaded and enabled automatically with supported models.
- Expanded the TTS and STT model lists with support for more languages.

### Changed

- llama.cpp now attempts an automatic version replacement if startup fails.
- Optimized translation-mode prompts to improve translation speed.
- If an official llama.cpp release is unavailable or broken, installation automatically tries earlier builds until a working release is found.
- Updated model loading for vision support. Existing installations automatically migrate their data to the new layout.
- Adjusted startup parameters to improve model responses.
- Context size is now a global setting. Changing it from glossary settings also updates the global setting.
- Reworked model loading to support paths containing Chinese characters and provide more flexible per-model configuration while preserving compatibility with existing installations. For example, the high-code model could use a 163840-token context without changing the default 16384-token context used by other models.
- Redesigned the glossary settings UI, including import and export workflows, for easier use.
- Adjusted context-related settings to prevent performance degradation during long sessions.
- Improved glossary matching so translations select glossary terms more accurately. The updated matching rules are documented in the glossary-entry help.
- Updated the Chinese-Spanish glossary to v1.0.1 and added Chinese-English, Russian-Georgian, Chinese-Georgian, Chinese-Serbian, Chinese-Croatian, and Chinese-Dutch glossaries.

### Fixed

- Fixed the default database not taking effect for some new installations.
- Fixed llama.cpp uninstallation failing because files remained locked.
- Fixed several additional minor issues.

## [3.2.2] - 2026-05-11

### Added

- Added an automatic update check at every application startup, with a prompt when an update is available.

### Changed

- Updated the TTS model download strategy.
- Added more comprehensive hardware detection during initial setup for smarter model selection.
- Added Quality Priority and Speed Priority options to initial model selection. Recommendations now account for the selected priority, operating system, hardware, and model requirements.
- Open Terminal is no longer installed by default.
- Made the `Low_EQ4` and `medium-low_Q6` models exclusive to Mac and hidden them on other systems.
- Added two models for lower-end hardware and higher translation quality, and renamed all models to make selection easier.
- Prevented low-spec systems from selecting models that are too large to run reliably.

## [3.2.1] - 2026-05-10

### Added

- Added simultaneous voice translation as a beta feature. For example, a Chinese speaker can be translated and played to another person in a foreign language, while the other person's speech is translated back into Chinese for display or playback.

### Changed

- Added a localized context-limit message showing the current token count and model limit, with guidance to shorten the input or increase context length in model settings.
- After a failed assistant response, users are no longer forced to create a new chat. The failed response is skipped so the conversation can continue.
- Removed all online-search modules because they required paid services. A different solution may be introduced later if needed.
- Glossaries can now be deleted, multiple terms can be selected and deleted together, and edited user glossaries are protected from updates. Editing an official glossary for the first time creates a personal copy automatically.
- Renamed the default official glossary to `glossary_es.json`, added an Official tag, and named it Chinese-Spanish Glossary in preparation for additional official glossaries.
- Restricted glossary use to matching language pairs to prevent words from unrelated languages appearing in translations. If a user selects English as the target language without an English glossary, AuraPro creates an empty English glossary for future entries.
- Added source-language configuration to glossaries and translation-related modes. Language pairs such as English-Japanese are now supported without requiring Chinese as the source language.
- Improved prompts for translation-related modes to handle Chinese input that uses spaces instead of punctuation.
- Improved prompts for translation-related modes so verbs and common nouns are not mistaken for untranslated names.
- Upgraded the user database to V3 and fully separated application features from user data. Future updates no longer replace the user database.
- Note: This database migration removes existing chat history and user-modified prompt settings. Glossaries and downloaded models are preserved.

## [3.1.4] - 2026-05-10

### Added

- Added audio settings for selecting microphones and speakers, making troubleshooting easier and providing a compatibility layer for future simultaneous voice translation.

### Changed

- Speech recognition now follows the same language settings as voice translation and detects the language automatically. For example, when Spanish is selected, it distinguishes Spanish from Chinese. When no translation mode is enabled, voice input defaults to Chinese recognition.
- Fixed voice input on local-area-network connections by switching to HTTPS and adding a self-signed certificate. Browsers can grant microphone permission after the user accepts the certificate warning.
- Removed all subscription and authorization-plan code from the WebUI.
- Removed all community-sharing code from the WebUI.

## [3.0.7] - 2026-05-08

### Added

- Added AuraPro WebUI to replace Open WebUI and enable deeper integration between the frontend and backend, including new features and a streamlined interface. AuraPro WebUI and Desktop use synchronized version numbers and will gradually be merged into one product.
- Added the Sherpa service to Desktop. Models can be configured in Settings, and speech-to-text and text-to-speech support faster voice translation and conversation while preparing for simultaneous voice translation.
- Added LAN mode, allowing household members to share computing resources. Users with lower-spec computers can connect to a more powerful computer on the same network. Multiple local and remote AI connections can be used simultaneously.
- Added shortcuts for extension features. Spotlight, voice input, and call shortcuts can be associated with specific modes such as Translation.
- Added dynamic glossary loading, selected-text correction, and one-click glossary generation. Users can import a list of required terms to generate a glossary automatically, correct mistranslated text by selection, and have corrections saved immediately without restarting. A spreadsheet-style glossary editor provides detailed term and setting management.
- Added support for multiple independent glossaries. Users can switch glossaries at any time, and the associated settings switch with them.
- Added one-click glossary import and export, including glossary version metadata, so high-quality glossaries can be shared between users or distributed through official updates.

### Changed

- On Linux, replaced `--in-process-gpu` with SwiftShader to fix blank WebViews.
- On macOS, fixed Spotlight behavior across virtual desktops so opening Spotlight no longer moves the user back to another desktop.
- Moved models into the `models` directory and automatically migrated the previous `models/huggingface` layout.
- On Windows, improved initial installation under CUDA to prevent slow networks from leaving the installation without a visible model.
- Updated startup to use AuraPro WebUI and fully decoupled the product from Open WebUI.
- Streamlined AuraPro WebUI and added the following capabilities:
- STT supports Whisper (secure), OpenAI API (secure or insecure), Sherpa (default and secure), and Web API (insecure).
- TTS supports Web API (insecure), OpenAI API (secure or insecure), and Sherpa (default and secure).
- Removed model ratings, leaderboards, and Arena features.
- Newly downloaded models are now reloaded automatically and can be used without restarting the application.
- Extension features such as Translation remain active when a new chat is created.
- Added near-streaming STT that displays interim transcription while recording and performs one final transcription from the complete audio after recording stops, balancing speed and accuracy.
- Separated user data from Translation, Interpretation, Learning, other extension modes, and glossaries at the database level. Future feature and glossary updates no longer overwrite user data.

### Fixed

- Improved installation handling so slow networks and long initial setup times no longer produce a false installation-failed message because of connection timeouts.
- Fixed incomplete retry behavior after an initial installation failure, which could start the application without installing the required models or components.
## [2.5.3] - 2026-05-04

### Added

- Open Terminal can now install Python automatically. If Python is missing when Open Terminal starts, AuraPro attempts a silent installation and displays progress in the UI.

### Changed

- On Linux, replaced `--disable-gpu` with `--in-process-gpu` to improve WebView rendering compatibility, especially under Wayland, while continuing to avoid most driver-related gray-screen issues.

## [2.5.2] - 2026-05-03

### Added

- Added a native context menu to WebViews with Cut, Copy, Paste, Undo, Redo, spelling suggestions, and Open Link in Browser. This also allows login pages to use system autofill and password managers.

### Changed

- On Linux, fixed gray embedded WebViews under Ubuntu and Wayland. Because Chromium software rendering was incompatible with native Wayland after GPU acceleration was disabled, WebViews now fall back to XWayland (X11).
- On Windows, fixed links that did not open. Links with `target="_blank"` now open in the system browser.
- Clarified that Linux installations require glibc 2.28 or later.

### Fixed

- Improved Windows OpenSSL compatibility by prepending the bundled Python directory to `PATH`, ensuring the correct OpenSSL DLLs are loaded instead of conflicting versions from Git for Windows, Anaconda, or other environments.
- Chat-response links now consistently open in the system browser rather than navigating inside the application or creating unwanted windows.

## [2.5.1] - 2026-05-01

### Added

- Added Translation Mode 2.0 Beta with translation and glossary support between any two languages. Streamlined decision logic improves response speed. Its internal version is 2.0.0. Because this is a major experimental update, it is available as a separately selectable mode.

### Changed

- Upgraded all three translation modes to internal version 1.4.0, improved their settings, prevented context-limit errors, and increased response speed.
- Disabled 3D acceleration in the UI to prevent rendering contention from freezing responses when mid-range systems run large models.
- Note: This release upgrades the database to internal version V2.1 and clears chat history from earlier releases.

## [2.5.0] - 2026-04-29

### Added

- Added cross-platform FFmpeg detection and installation for Windows, Linux, and macOS on x64 and ARM64. Missing audio-processing components are installed automatically into the bundled Python environment.

### Changed

- Upgraded the three translation modes to internal version 1.3.1, improving response speed and resolving crashes and stalls after prolonged use.
- Upgraded the database to V2, streamlined the interface, and improved startup and response speed. Note: This migration removes chat history from earlier releases.
- Updated uninstallation behavior. Manual uninstallation removes user settings and cache data, while installer-based upgrades continue to preserve user data.
- Added a one-time data migration that replaces core data files to synchronize built-in features. It runs only once for this upgrade.
- Injected the `USER_AGENT` environment variable to eliminate identification warnings from LangChain and related components.

## [2.4.4] - 2026-04-28

### Changed

- Added native Windows ARM64 support for Snapdragon devices and Linux ARM64 support for systems such as Raspberry Pi and DGX Spark.
- On Linux, fully disabled the GPU process to prevent shared-memory allocation crashes and gray screens on affected systems.
- Updated release workflows for automated multi-platform builds and merged auto-update metadata.

### Fixed

- Spotlight no longer opens the main window when it loses focus or is closed manually. The main window is restored only after a query is submitted.

## [2.4.3] - 2026-04-28

### Changed

- Restored full automatic CUDA Toolkit installation. NVIDIA's network installer handles component-specific reduced installations inconsistently and could stop unexpectedly, so AuraPro again installs the complete toolkit for more reliable setup.

## [2.4.1] - 2026-04-28

### Changed

- Improved NVIDIA inference performance on Windows with automatic detection and silent installation of CUDA 13.2 Toolkit.
- Strengthened CUDA version validation. Only CUDA 13.1 or 13.2 is accepted in this release, and older versions are upgraded automatically.
- Removed the experimental feature that automatically enabled extension modes from shortcuts because unreliable UI state detection caused repeated false triggers. It may return after upstream compatibility improves.
- Fixed 0 KB model files and application hangs that could occur when a model download began immediately after CUDA installation.
- Added automatic cleanup of temporary CUDA installer files to reduce disk usage.
## [2.3.2] - 2026-04-27

### Changed

- Added shortcut-triggered extension features. Spotlight, voice input, and call shortcuts can be associated with specific features such as Translation, Interpretation, and the code interpreter. This feature was experimental and not recommended for general use.
- Shortcut activation can now enable the associated extension mode in the background without requiring the user to select it manually. This feature was experimental and not recommended for general use.
- On Linux, disabled GPU compositing to prevent gray or blank WebViews.
- Added automatic renderer-process recovery to improve stability across Linux configurations and unexpected failures.
- Fixed Linux AppImage crashes caused by `/dev/shm` permissions.

## [2.2.6] - 2026-04-26

### Changed

- Fixed the Copy button in the embedded AuraPro WebView by improving clipboard permission handling.
- Enhanced the service log panel. Selected log text can now be copied with Ctrl+C on Windows and Linux or Command+C on macOS.

## [2.2.5] - 2026-04-26

### Changed

- Fixed incomplete downloaded-model lists in Settings. AuraPro now scans the model directory automatically and displays every downloaded model.
- Added atomic writes for the model manifest to prevent data loss after interrupted operations.

## [2.2.4] - 2026-04-26

### Changed

- Improved hardware detection on Windows. CUDA or Vulkan is now selected automatically by default for better inference performance.
- Fixed Python installation failures during Windows initialization by improving error reporting and installation recovery, including stopping conflicting processes and repairing pip.

## [2.2.3] - 2026-04-26

### Changed

- Improved hardware detection and model recommendations for the unified-memory architecture used by Apple silicon Macs.
- Fixed downloaded models missing their filename extension and therefore failing to run.

## [2.2.2] - 2026-04-26

### Changed

- Changed the default configuration path to prevent data loss during upgrades.
- Updated default settings to address Bitdefender false positives.
- Added documentation for the confirmed Bitdefender false-positive workaround.

## [2.2.1] - 2026-04-26

### Changed

- Fixed the macOS application logo.
- Known issue: On some Windows systems, Bitdefender may incorrectly identify and delete installed files, making AuraPro unusable. This appears to be caused by Bitdefender configuration.

## [2.2.0] - 2026-04-26

### Changed

- Added two optional models.
- Updated all model recommendation rules.
- Added a dedicated configuration for Macs with 8 GB of unified memory.
- Improved data storage so upgrades preserve downloaded models and settings in the operating system's application-data directory.
- Known issue: On some Windows systems, Bitdefender may incorrectly identify and delete installed files, making AuraPro unusable. This appears to be caused by Bitdefender configuration.

## [2.1.0] - 2026-04-25

### Changed

- Improved automatic hardware detection for more accurate recommended settings.

## [2.0.10] - 2026-04-24

### Changed

- Reworked settings-database parameters for the updated UI.
- Added a model download list for easier model selection.
- Improved the initial setup screen with a default-model selector.
- Optimized default startup parameters for better runtime efficiency.
- Added a setting for custom context length, defaulting to 16384.
- Added a setting for selecting the compute backend, with automatic detection as the default.
- Changed the model download location for easier management.
- Improved process management to reduce stalls after prolonged use.
- Fixed all known issues reported between versions 2.0.0 and 2.0.10.

## [2.0.0] - 2026-04-23

### Changed

- Fixed startup errors and incorrect update prompts that prevented some users from using AuraPro.
- Rebuilt the application around a new desktop UI, removed the external console window, and moved all commands into managed background processes.
- Reduced the installer size from approximately 1.5 GB to 0.1 GB.
- Added update checks, automatic updates, release notes, service logs, and global shortcuts.
- Replaced voice input with a more efficient implementation.
- Added support for Windows, two macOS builds, and Linux.
## [1.2.1] - 2026-04-22

### Changed

- On Windows, replaced the full CUDA installation with a reduced package to shorten initial setup.
- On Windows, fixed incorrect VRAM detection when an integrated GPU was present.
- On Windows, changed startup-script encoding to resolve compatibility issues.
- Known issue: AuraPro may not run when the Windows user path contains Chinese characters.

## [1.2.0] - 2026-04-20

### Added

- Added the mid-low E6 model, optimized primarily for systems with 16 GB of memory.

### Changed

- Fixed Windows VRAM sometimes being detected as 4 GB in version 1.1.1.
- Added a forced translation-language lock to prevent translation mode from being bypassed in version 1.1.2.
- Improved prompts to reduce excessive annotations for words that are not person or place names in version 1.1.2.
- Imported additional domain-specific terminology and updated the glossary in version 1.1.2.
- Improved default Windows installation settings in version 1.1.3.
- Replaced the Windows logo with a rounded version in version 1.1.3.
- Added an extra model-download interface for Windows testing. macOS support was planned for a later release.
- Added optional `super-high_Q5` and `high-code_Q4` models. The former targets systems above the high-tier recommendation, while the latter targets coding and general Chinese-language workloads.
- Optimized low-tier Mac settings for 8 GB systems and added the dedicated `low_EQ4` model.

## [1.1.1] - 2026-04-18

### Added

- Added the mid-low E6 model, optimized primarily for systems with 16 GB of memory.

### Changed

- Included the same features as Windows v1.1.0 plus additional Mac-specific updates.
- Updated the logo to follow the macOS 26 design style. The Windows logo was scheduled to receive the same treatment in the next release.

### Known Issues

- Macs with 8 GB of memory were not yet supported, and a solution was still being investigated.
- Intel-based Macs were also expected to be unsupported.

## [1.1.0] - 2026-04-17

### Changed

- Fixed long messages being truncated when sent.
- Improved prompts to prevent occasional unwanted content shortening.
- Added a more complete Windows installer and prepared incremental patches so future updates would not require uninstalling the previous version.
- Added a temporary application logo.
- Improved the fully automated installation flow so it is easier for non-technical and older users.

## [1.0.1] - 2026-04-17

### Changed

- Added installation and startup integrity checks to prevent launches with missing files.
- Improved the default system prompt to produce complete and accurate responses.
- Announced the upcoming macOS release.

## [1.0.0] - 2026-04-08

### Added

- Added automatic hardware detection during installation and selected a corresponding low-, medium-, or high-tier model. Available models included high-tier Q4, medium-high IQ4, medium Q2, and low E4.

### Changed

- Configured the llama.cpp backend to download the latest available release automatically.
- Improved the dynamic glossary and changed prompts to an open language-independent design.
- Added Translation, Learning, and Interpretation modes based on the existing glossary mode.

## [0.0.4] - 2026-04-03

### Changed

- Improved installation so CUDA would no longer be requested repeatedly.
- Retained only the larger 4.7-flash and 4-26b models for testing.
- Added prompt improvements to make translation results more usable.
- Added a dynamic glossary and improved handling of specialized terminology.
- Updated the llama.cpp backend to build b8660.
- 2026-03-31:
- Development was temporarily paused because test results were poor, and testing switched to API mode. Glossary support was tested during this period but was not released.

## [0.0.3] - 2026-03-30

### Changed

- Added TX1.8b, TX7b, 3.5-2b, and G3-4B models for CPU-only systems.
- Completed the first working macOS build.

## [0.0.2] - 2026-03-28

### Changed

- Reworked the project and fixed installation failures and files being stored outside the project directory.

## [0.0.1] - 2026-03-27

### Added

- Completed the first working release with the 3.5-4b and 3.5-9b models.