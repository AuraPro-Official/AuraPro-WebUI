"""Desktop-managed Pi RPC runtime. No Node or Pi binaries are shipped in the wheel."""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

_sessions: dict[str, PiSession] = {}
_subscribers: dict[str, set[asyncio.Queue]] = {}
_creation_lock = asyncio.Lock()
_workspace_locks: dict[str, asyncio.Lock] = {}
_IGNORED = {
    '.git',
    'node_modules',
    '.venv',
    '__pycache__',
    '.pi',
    '.tmp',
    'dist',
    '.svelte-kit',
    'AppData',
    '.cache',
    '.npm',
    '.codex',
    '.conda',
}


def descriptor() -> dict:
    file = os.getenv('AURAPRO_PI_RUNTIME_FILE') or os.getenv('AURAPRO_OPENCODE_RUNTIME_FILE', '')
    if not file:
        raise RuntimeError('Agent is not configured. Install and start Agent in Desktop settings.')
    data = json.loads(Path(file).read_text(encoding='utf-8'))
    if data.get('engine') != 'pi' or data.get('version') != 2:
        raise RuntimeError('The Agent runtime descriptor is invalid. Restart Agent in Desktop settings.')
    if not Path(data['executable']).is_file():
        raise RuntimeError('The installed Agent executable is missing.')
    if data.get('ownerPid'):
        try:
            if os.name == 'nt':
                import ctypes

                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.OpenProcess.restype = ctypes.c_void_p
                handle = kernel.OpenProcess(0x1000, False, int(data['ownerPid']))
                if not handle:
                    raise OSError('Desktop process unavailable.')
                code = ctypes.c_ulong()
                kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
                kernel.CloseHandle(ctypes.c_void_p(handle))
                if code.value != 259:
                    raise OSError('Desktop process exited.')
            else:
                os.kill(int(data['ownerPid']), 0)
        except OSError as error:
            raise RuntimeError('Desktop is no longer running. Restart Agent from Desktop settings.') from error
    return data


def _state_dir(data: dict) -> Path:
    directory = Path(data['agentDir']) / 'aurapro-sessions'
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _safe_path(directory: str, relative: str) -> Path:
    root = Path(directory).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise RuntimeError('Snapshot path leaves the selected workspace.')
    return target


def snapshot(directory: str) -> tuple[dict[str, str], list[str]]:
    """Bounded byte snapshots; symlinks are deliberately never followed."""
    result: dict[str, str] = {}
    skipped: list[str] = []
    total = 0
    count = 0
    deadline = time.monotonic() + 3
    for parent, directories, files in os.walk(directory, followlinks=False):
        if time.monotonic() >= deadline or count >= 10000:
            skipped.append('[snapshot scan limit reached]')
            break
        directories[:] = [
            name for name in directories if name not in _IGNORED and not (Path(parent) / name).is_symlink()
        ]
        for name in files:
            if time.monotonic() >= deadline or count >= 10000:
                skipped.append('[snapshot scan limit reached]')
                return result, skipped
            file = Path(parent) / name
            relative = file.relative_to(directory).as_posix()
            count += 1
            try:
                size = file.stat().st_size
                if file.is_symlink() or size > 2 * 1024 * 1024 or total + size > 64 * 1024 * 1024 or count > 10000:
                    skipped.append(relative)
                    continue
                content = file.read_bytes()
                total += len(content)
                result[relative] = base64.b64encode(content).decode('ascii')
            except OSError:
                skipped.append(relative)
    return result, skipped


def changes(before: dict, after: dict, skipped: list[str]) -> list[dict]:
    result = []
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) == after.get(name) or name in skipped:
            continue
        if '[snapshot scan limit reached]' in skipped and (name not in before or name not in after):
            # A partial scan cannot establish whether a missing file was added/deleted.
            continue
        old = base64.b64decode(before.get(name, ''))
        new = base64.b64decode(after.get(name, ''))
        patch = ''
        try:
            old_text, new_text = old.decode('utf-8'), new.decode('utf-8')
            if '\x00' not in old_text + new_text:
                lines = difflib.unified_diff(
                    old_text.splitlines(True), new_text.splitlines(True), fromfile=f'a/{name}', tofile=f'b/{name}'
                )
                patch = ''.join(
                    line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in lines
                )[:120000]
        except UnicodeDecodeError:
            pass
        result.append(
            {
                'file': name,
                'path': name,
                'status': 'added' if name not in before else 'deleted' if name not in after else 'modified',
                'patch': patch,
                'before': old.decode('utf-8', errors='replace')[:120000],
                'after': new.decode('utf-8', errors='replace')[:120000],
                'additions': sum(line.startswith('+') and not line.startswith('+++') for line in patch.splitlines()),
                'deletions': sum(line.startswith('-') and not line.startswith('---') for line in patch.splitlines()),
            }
        )
    return result


async def emit(directory: str, kind: str, session_id: str, **properties: Any) -> None:
    event = {'type': kind, 'properties': {'sessionID': session_id, **properties}}
    for queue in tuple(_subscribers.get(directory, set())):
        if queue.full():
            # Backpressure must not stall the RPC reader and permission replies.
            queue.get_nowait()
        queue.put_nowait(event)


class EventStream:
    status = 200

    def __init__(self, directory: str):
        self.directory = str(Path(directory).resolve(strict=True))
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        _subscribers.setdefault(self.directory, set()).add(self.queue)
        self.content = self

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.queue.get()
        return ('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()

    def close(self):
        _subscribers.get(self.directory, set()).discard(self.queue)


class PiSession:
    def __init__(self, data: dict, session_id: str, directory: str, saved: dict | None = None, interactive=True):
        self.data, self.id, self.directory = data, session_id, directory
        self.file = _state_dir(data) / f'{session_id}.json'
        self.saved = saved or {'id': session_id, 'directory': directory, 'turns': []}
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.tasks: list[asyncio.Task] = []
        self.diagnostics: dict = {}
        self.probe: dict = {}
        self.live_text = ''
        self.live_id = ''
        self.busy = False
        self.pending_model_error = ''
        self.interactive = interactive
        self.mode = 'build'
        self.model_environment: dict[str, str] = {}
        self.ui: dict[str, dict] = {}
        self.ui_expires: dict[str, float] = {}
        self.answer_lock = asyncio.Lock()
        self.finish_lock = asyncio.Lock()
        self.closing = False
        self.before: dict | None = None
        self.skipped: list[str] = []
        self.prompt_lock = _workspace_locks.setdefault(directory, asyncio.Lock())
        self.compacting = False
        self.maintenance_task: asyncio.Task | None = None
        self.live_tools: dict[str, dict] = {}
        self.queue: dict = {'steering': [], 'followUp': []}
        self.stats: dict = {}
        self.stats_at = 0.0
        self.save_handle = None
        self.saved.setdefault('activity', [])
        self.saved.setdefault('sequence', 0)
        self.saved.setdefault('auto_compaction', True)
        self.saved.setdefault('todos', [])
        for item in self.saved['activity']:
            if item.get('kind') == 'tool' and item.get('tool_id'):
                self.live_tools[item['tool_id']] = {**item, 'id': item['tool_id']}

    def record(self, kind: str, **values):
        self.saved['sequence'] += 1
        item = {**values, 'id': self.saved['sequence'], 'time': time.time(), 'kind': kind}
        self.saved['activity'].append(item)
        self.saved['activity'] = self.saved['activity'][-500:]
        if self.save_handle is None:
            self.save_handle = asyncio.get_running_loop().call_later(0.5, self.save)

    async def activity(self, after=0):
        for ui_id, deadline in list(self.ui_expires.items()):
            if time.monotonic() >= deadline:
                self.ui.pop(ui_id, None)
                self.ui_expires.pop(ui_id, None)
        if self.process and self.process.returncode is None and time.monotonic() - self.stats_at > 3:
            self.stats_at = time.monotonic()
            try:
                self.stats = await self.command('get_session_stats', timeout=3) or {}
            except RuntimeError:
                pass
        items = self.saved['activity']
        if after > self.saved['sequence']:
            after = 0
        return {
            'session_id': self.id,
            'busy': self.busy,
            'compacting': self.compacting,
            'auto_compaction': self.saved['auto_compaction'],
            'cursor': self.saved['sequence'],
            'truncated': bool(items and after and after < items[0]['id'] - 1),
            'items': [item for item in items if item['id'] > after],
            'requests': list(self.ui.values()),
            'tools': list(self.live_tools.values()),
            'text': self.live_text[-16000:],
            'todos': self.saved['todos'],
            'queue': self.queue,
            'stats': self.stats,
            'error': self.saved.get('last_error', ''),
        }

    def save(self):
        if self.save_handle is not None:
            self.save_handle.cancel()
            self.save_handle = None
        temporary = self.file.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.saved), encoding='utf-8')
        temporary.replace(self.file)

    async def start(self, mode='build'):
        if self.process and self.process.returncode is None:
            if mode == self.mode:
                return
            if self.busy:
                raise RuntimeError('Cannot change mode while Agent is running.')
            await self.close()
        self.mode = mode
        self.closing = False
        self.diagnostics = {}
        self.probe = {}
        if self.model_environment.get('AURAPRO_WEBUI_MODEL'):
            model = json.loads(self.model_environment['AURAPRO_WEBUI_MODEL'])
            window = int(model.get('contextWindow') or 32768)
            # Keep summary/recent-history budgets proportional to the selected model.
            # Agent defaults retain 20k tokens, which can prevent useful small-window recovery.
            settings_file = Path(self.data['agentDir']) / 'settings.json'
            settings = json.loads(settings_file.read_text(encoding='utf-8')) if settings_file.exists() else {}
            compaction = settings.setdefault('compaction', {})
            overrides = compaction.setdefault('modelOverrides', {})
            overrides[f'aurapro-webui/{model["id"]}'] = {
                'reserveTokens': min(16384, max(256, window // 4)),
                'keepRecentTokens': min(12000, max(512, window // 5)),
            }
            temporary = settings_file.with_suffix(f'.{uuid.uuid4()}.tmp')
            temporary.write_text(json.dumps(settings, ensure_ascii=False), encoding='utf-8')
            temporary.replace(settings_file)
        env = {
            **os.environ,
            **self.data.get('environment', {}),
            **self.model_environment,
            'PI_CODING_AGENT_DIR': self.data['agentDir'],
            'AURAPRO_PI_PLAN': '1' if mode == 'plan' else '0',
        }
        args = [
            self.data['executable'],
            *self.data.get('arguments', []),
            '--mode',
            'rpc',
            '--session-id',
            self.id,
            '--session-dir',
            str(_state_dir(self.data) / 'transcripts'),
            '--no-approve',
            '--offline',
        ]
        if mode == 'plan':
            args += ['--tools', 'read,grep,find,ls,ask_user,update_plan']
        self.process = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.directory,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        self.tasks = [
            asyncio.create_task(self.read()),
            asyncio.create_task(self.drain_errors()),
            asyncio.create_task(self.watch_runtime()),
        ]
        state = await self.command('get_state', timeout=30)
        if isinstance(state, dict):
            self.saved['session_file'] = state.get('sessionFile')
        await self.command('set_auto_compaction', enabled=self.saved['auto_compaction'])
        await self.command('set_auto_retry', enabled=True)
        if not self.diagnostics.get('auraproDiagnostics'):
            await self.close()
            raise RuntimeError(
                'The AuraPro Agent bridge did not load. Restart Agent in Desktop settings and check extension installation.'
            )

    async def watch_runtime(self):
        while self.process and self.process.returncode is None:
            await asyncio.sleep(2)
            try:
                current = descriptor()
                if current != self.data:
                    raise RuntimeError('Agent runtime settings changed. Start a new turn.')
            except Exception:
                await self.close()
                return

    async def drain_errors(self):
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            detail = line.decode('utf-8', errors='replace').strip()[:2000]
            self.saved['last_error'] = detail
            self.record('diagnostic', text=detail)

    async def send(self, value: dict):
        if not self.process or self.process.returncode is not None or not self.process.stdin:
            raise RuntimeError('Agent process is not running.')
        self.process.stdin.write(json.dumps(value, ensure_ascii=False).encode('utf-8') + b'\n')
        await self.process.stdin.drain()

    async def command(self, kind: str, timeout=30, **values: Any):
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({'id': request_id, 'type': kind, **values})
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as error:
            raise RuntimeError(
                f'Agent command {kind} did not respond within {timeout} seconds. Check the model connection and extension setup.'
            ) from error
        finally:
            self.pending.pop(request_id, None)

    async def read(self):
        assert self.process and self.process.stdout
        process = self.process
        try:
            while line := await process.stdout.readline():
                event = json.loads(line)
                kind = event.get('type')
                if kind == 'response':
                    future = self.pending.get(event.get('id'))
                    if future and not future.done():
                        if event.get('success'):
                            future.set_result(event.get('data'))
                        else:
                            future.set_exception(RuntimeError(str(event.get('error') or 'Agent command failed.')))
                elif kind == 'extension_ui_request':
                    try:
                        diagnostic = json.loads(event.get('message') or '')
                    except (ValueError, TypeError):
                        diagnostic = {}
                    if diagnostic.get('auraproDiagnostics'):
                        self.diagnostics = diagnostic
                    elif diagnostic.get('auraproProbe'):
                        self.probe = diagnostic
                    elif diagnostic.get('auraproPlan'):
                        self.saved['todos'] = diagnostic.get('steps', [])[:30]
                        self.record('plan', steps=self.saved['todos'])
                    else:
                        if event.get('method') in {'confirm', 'select', 'input', 'editor'} and (
                            not self.interactive or not _subscribers.get(self.directory)
                        ):
                            await self.send({'type': 'extension_ui_response', 'id': event['id'], 'cancelled': True})
                        else:
                            if event.get('method') in {'confirm', 'select', 'input', 'editor'}:
                                self.ui[event['id']] = event
                                if isinstance(event.get('timeout'), (int, float)) and event['timeout'] > 0:
                                    self.ui_expires[event['id']] = time.monotonic() + event['timeout'] / 1000
                                self.record('question', request=event)
                            elif event.get('method') == 'notify':
                                self.record(
                                    'notice',
                                    text=str(event.get('message') or '')[:6000],
                                    status=event.get('notifyType'),
                                )
                            await emit(self.directory, 'pi.ui', self.id, request=event)
                elif kind == 'agent_start':
                    self.busy = True
                    self.saved.pop('last_error', None)
                    self.record('started')
                    await emit(self.directory, 'session.status', self.id, status={'type': 'busy'})
                elif kind == 'message_start' and event.get('message', {}).get('role') == 'assistant':
                    self.live_text = ''
                    self.live_id = f'pi_live_{event["message"].get("timestamp", time.time_ns())}'
                    await emit(self.directory, 'message.part.updated', self.id)
                elif kind == 'message_update':
                    update = event.get('assistantMessageEvent', {})
                    if update.get('type') == 'text_delta':
                        self.live_text += update.get('delta', '')
                    await emit(self.directory, 'message.part.updated', self.id)
                elif kind == 'message_end':
                    message = event.get('message') or {}
                    if message.get('role') == 'assistant':
                        content = message.get('content', [])
                        text = (
                            content
                            if isinstance(content, str)
                            else '\n'.join(block.get('text', '') for block in content if block.get('type') == 'text')
                        )
                        if text:
                            self.record('message', text=text[:20000])
                    self.live_text = ''
                    self.live_id = ''
                    if event.get('message', {}).get('stopReason') == 'error':
                        # Agent may compact/retry this error before the run settles.
                        self.pending_model_error = event['message'].get('errorMessage') or 'Agent model request failed.'
                    else:
                        if event.get('message', {}).get('role') == 'assistant':
                            self.pending_model_error = ''
                        await emit(self.directory, 'message.part.updated', self.id)
                elif kind in {'compaction_start', 'compaction_end'}:
                    self.compacting = kind == 'compaction_start'
                    result = event.get('result') or {}
                    self.record(
                        kind,
                        reason=event.get('reason'),
                        error=event.get('errorMessage'),
                        tokens_before=result.get('tokensBefore'),
                        tokens_after=result.get('estimatedTokensAfter'),
                        text=event.get('errorMessage'),
                    )
                    if result:
                        self.pending_model_error = ''
                        self.stats = {}
                        self.stats_at = time.monotonic()
                    await emit(self.directory, 'pi.compaction', self.id, **event)
                elif kind in {'tool_execution_start', 'tool_execution_update', 'tool_execution_end'}:
                    tool_id = str(event.get('toolCallId') or '')
                    tool = self.live_tools.setdefault(
                        tool_id, {'id': tool_id, 'name': event.get('toolName'), 'input': event.get('args', {})}
                    )
                    result = event.get('result') or event.get('partialResult') or {}
                    tool['status'] = (
                        ('error' if event.get('isError') or result.get('isError') else 'completed')
                        if kind == 'tool_execution_end'
                        else 'running'
                    )
                    tool['output'] = '\n'.join(
                        str(block.get('text', '')) for block in result.get('content', []) if block.get('type') == 'text'
                    )[-20000:]
                    if len(self.live_tools) > 100:
                        self.live_tools.pop(next(iter(self.live_tools)))
                    if kind != 'tool_execution_update':
                        self.record('tool', tool_id=tool_id, **tool)
                    await emit(self.directory, 'message.part.updated', self.id)
                elif kind == 'queue_update':
                    self.queue = {key: event.get(key, []) for key in ('steering', 'followUp')}
                elif kind in {'auto_retry_start', 'auto_retry_end', 'extension_error'}:
                    self.record(
                        kind,
                        text=str(event.get('errorMessage') or event.get('finalError') or event.get('error') or '')[
                            :4000
                        ],
                        attempt=event.get('attempt'),
                    )
                elif kind == 'agent_settled':
                    # The RPC reader must stay free to receive the get_messages response.
                    task = asyncio.create_task(self.finish())
                    self.tasks.append(task)
                    task.add_done_callback(
                        lambda completed: self.tasks.remove(completed) if completed in self.tasks else None
                    )
                else:
                    await emit(self.directory, 'message.part.updated', self.id)
        except (ValueError, OSError) as error:
            self.saved['last_error'] = str(error)
        finally:
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError(self.saved.get('last_error') or 'Agent process exited.'))
            if self.busy and not self.closing:
                self.busy = False
                self.compacting = False
                self.ui.clear()
                self.ui_expires.clear()
                self.queue = {'steering': [], 'followUp': []}
                self.record('error', text=self.saved.get('last_error') or 'Agent process exited before completion.')
                await emit(
                    self.directory,
                    'session.error',
                    self.id,
                    error=self.saved.get('last_error') or 'Agent process exited before completion.',
                )

    async def finish(self):
        async with self.finish_lock:
            if not self.busy:
                return
            await self._finish()

    async def _finish(self):
        try:
            if self.before is not None:
                after, skipped = await asyncio.to_thread(snapshot, self.directory)
                messages = await self.messages()
                user_id = next(
                    (item['info']['id'] for item in reversed(messages) if item['info']['role'] == 'user'), ''
                )
                diffs = changes(self.before, after, self.skipped + skipped)
                names = {item['file'] for item in diffs}
                turn = {
                    'message_id': user_id,
                    'before': {name: self.before.get(name) for name in names},
                    'after': {name: after.get(name) for name in names},
                    'diffs': diffs,
                    'skipped': self.skipped + skipped,
                }
                self.saved['turns'].append(turn)
                self.before = None
                self.save()
                await emit(self.directory, 'session.diff', self.id, diff=diffs)
                if turn['skipped']:
                    await emit(
                        self.directory,
                        'pi.ui',
                        self.id,
                        request={
                            'method': 'notify',
                            'message': 'Some workspace files were excluded from snapshots (large files, dependencies or inaccessible paths). Revert covers only the displayed files.',
                            'notifyType': 'warning',
                        },
                    )
            self.busy = False
            await self.dismiss_dialogs()
            if self.pending_model_error:
                self.record('error', text=self.pending_model_error)
                await emit(self.directory, 'session.error', self.id, error=self.pending_model_error)
                return
            self.record('settled')
            await emit(self.directory, 'session.idle', self.id)
        except Exception as error:
            self.busy = False
            self.record('error', text=str(error))
            await self.dismiss_dialogs()
            await emit(self.directory, 'session.error', self.id, error=str(error))

    async def messages(self):
        response = await self.command('get_messages')
        messages = response.get('messages', []) if isinstance(response, dict) else []
        result = []
        parent = ''
        tools: dict[str, dict] = {}
        for index, message in enumerate(messages):
            role = message.get('role')
            if role in {'user', 'assistant'}:
                message_id = f'pi_{message.get("timestamp", index)}_{role}'
                if role == 'user':
                    parent = message_id
                content = message.get('content', [])
                if isinstance(content, str):
                    content = [{'type': 'text', 'text': content}]
                parts = []
                for part in content:
                    if part.get('type') == 'text':
                        parts.append({'type': 'text', 'text': part.get('text', '')})
                    elif part.get('type') == 'toolCall':
                        tool = {
                            'id': part['id'],
                            'type': 'tool',
                            'tool': part['name'],
                            'state': {'status': 'running', 'input': part.get('arguments', {})},
                        }
                        tools[part['id']] = tool
                        parts.append(tool)
                result.append(
                    {
                        'info': {
                            'id': message_id,
                            'role': role,
                            'parentID': parent,
                            'stopReason': message.get('stopReason'),
                            'errorMessage': message.get('errorMessage'),
                            'usage': message.get('usage'),
                        },
                        'parts': parts,
                    }
                )
            elif role == 'toolResult' and message.get('toolCallId') in tools:
                tools[message['toolCallId']]['state'].update(
                    {
                        'status': 'error' if message.get('isError') else 'completed',
                        'output': message.get('content'),
                        'metadata': message.get('details', {}),
                    }
                )
        for tool in tools.values():
            live = self.live_tools.get(tool['id'])
            if live and live.get('output') and tool['state']['status'] == 'running':
                tool['state']['output'] = [{'type': 'text', 'text': live['output']}]
        if self.live_text:
            result.append(
                {
                    'info': {'id': self.live_id, 'role': 'assistant', 'parentID': parent},
                    'parts': [{'type': 'text', 'text': self.live_text}],
                }
            )
        return result

    async def dismiss_dialogs(self):
        for ui_id in list(self.ui):
            try:
                await self.send({'type': 'extension_ui_response', 'id': ui_id, 'cancelled': True})
            except (RuntimeError, OSError):
                pass
        self.ui.clear()
        self.ui_expires.clear()

    async def answer(self, payload):
        async with self.answer_lock:
            await self._answer(payload)

    async def _answer(self, payload):
        event = self.ui.get(payload.get('id'))
        if not event or time.monotonic() >= self.ui_expires.get(payload.get('id'), float('inf')):
            self.ui.pop(payload.get('id'), None)
            self.ui_expires.pop(payload.get('id'), None)
            raise RuntimeError('This Agent dialog expired.')
        answer = {'type': 'extension_ui_response', 'id': event['id']}
        if payload.get('cancelled') is True:
            answer['cancelled'] = True
        elif event['method'] == 'confirm':
            if not isinstance(payload.get('confirmed'), bool):
                raise RuntimeError('A confirmation must be accepted or declined.')
            answer['confirmed'] = payload['confirmed']
        else:
            value = payload.get('value')
            if not isinstance(value, str) or len(value) > 20000:
                raise RuntimeError('Enter a valid Agent response.')
            if event['method'] == 'select' and value not in event.get('options', []):
                raise RuntimeError('Choose one of the available options.')
            answer['value'] = value
        await self.send(answer)
        self.ui.pop(event['id'], None)
        self.ui_expires.pop(event['id'], None)
        self.record(
            'answer',
            request_id=event['id'],
            text=answer.get('value')
            or ('Cancelled' if answer.get('cancelled') else 'Approved' if answer.get('confirmed') else 'Declined'),
            confirmed=answer.get('confirmed'),
            cancelled=answer.get('cancelled', False),
        )

    async def close(self):
        if self.closing:
            return
        self.closing = True
        await self.dismiss_dialogs()
        current = asyncio.current_task()
        process = self.process
        if process and process.returncode is None:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                process.kill()
                await process.wait()
        tasks = [task for task in self.tasks if task is not current]
        for task in tasks:
            if task is not current:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks = [current] if current in self.tasks else []
        self.process = None
        self.compacting = False
        self.busy = False
        self.save()


async def get_session(session_id: str, directory: str, *, start=True) -> PiSession:
    uuid.UUID(session_id)
    directory = str(Path(directory).resolve(strict=True))
    async with _creation_lock:
        if session_id not in _sessions:
            data = descriptor()
            file = _state_dir(data) / f'{session_id}.json'
            saved = json.loads(file.read_text(encoding='utf-8'))
            _sessions[session_id] = PiSession(data, session_id, saved['directory'], saved)
        session = _sessions[session_id]
        if session.directory != directory:
            raise RuntimeError('Agent session belongs to a different workspace.')
        current = descriptor()
        if current != session.data:
            await session.close()
            session.data = current
        if start:
            await session.start(session.mode)
        return session


async def _vcs(directory: str):
    try:
        process = await asyncio.create_subprocess_exec(
            'git',
            '-C',
            directory,
            'rev-parse',
            '--show-toplevel',
            '--abbrev-ref',
            'HEAD',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        output, _ = await asyncio.wait_for(process.communicate(), 5)
        lines = output.decode(errors='replace').splitlines()
        return {'root': lines[0], 'branch': lines[1]} if len(lines) > 1 else {}
    except TimeoutError:
        process.kill()
        await process.wait()
        return {}
    except OSError:
        return {}


async def request(method: str, endpoint: str, directory: str | None, payload: dict | None, query: dict | None):
    data = descriptor()
    directory = str(Path(directory or data['cwd']).resolve(strict=True))
    payload, query = payload or {}, query or {}
    if endpoint == '/global/health':
        return {'healthy': True, 'version': data['piVersion']}
    if endpoint == '/path':
        return {'directory': directory}
    if endpoint == '/vcs':
        return await _vcs(directory)
    if endpoint == '/agent':
        return [
            {
                'name': mode,
                'mode': 'primary',
                'description': 'Read-only workspace analysis' if mode == 'plan' else 'Agent with installed extensions',
            }
            for mode in ('build', 'plan')
        ]
    if endpoint == '/provider':
        probe = PiSession(data, str(uuid.uuid4()), directory, interactive=False)
        try:
            await probe.start()
            response = await probe.command('get_available_models')
            groups: dict[str, dict] = {}
            for model in (response or {}).get('models', []):
                provider = model['provider']
                groups.setdefault(provider, {'id': provider, 'name': provider, 'models': {}})['models'][model['id']] = (
                    model
                )
            return {'all': list(groups.values()), 'connected': list(groups), 'default': {}}
        finally:
            await probe.close()
    if endpoint == '/session' and method == 'POST':
        session_id = str(uuid.uuid4())
        session = PiSession(data, session_id, directory)
        session.save()
        _sessions[session_id] = session
        await session.start()
        return {'id': session_id}
    if endpoint == '/session/status':
        return {
            key: {'type': 'busy' if session.busy else 'idle'}
            for key, session in _sessions.items()
            if session.directory == directory
        }
    if endpoint == '/file/status':
        return []
    parts = endpoint.strip('/').split('/')
    if len(parts) < 2 or parts[0] != 'session':
        raise RuntimeError(f'Unsupported Agent endpoint: {endpoint}')
    action = parts[2] if len(parts) > 2 else ''
    session = await get_session(
        parts[1], directory, start=action in {'', 'message', 'prompt_async'} and method != 'DELETE'
    )
    if not action:
        if method == 'DELETE':
            await session.close()
            _sessions.pop(session.id, None)
        return {'id': session.id}
    if action == 'message':
        return await session.messages()
    if action == 'prompt_async':
        async with session.prompt_lock:
            if any((item.busy or item.compacting) and item.directory == directory for item in _sessions.values()):
                raise RuntimeError('An Agent task is already running in this workspace. Wait or stop it first.')
            if session.saved.get('reverted'):
                raise RuntimeError(
                    'Restore the reverted files or reset this conversation before starting another task.'
                )
            await session.start(payload.get('agent', 'build'))
            model = payload.get('model')
            if model:
                await session.command('set_model', provider=model['providerID'], modelId=model['modelID'])
            session.busy = True
            session.pending_model_error = ''
            session.live_tools.clear()
            session.saved['todos'] = []
            try:
                session.before, session.skipped = await asyncio.to_thread(snapshot, directory)
                message = '\n'.join(part.get('text', '') for part in payload.get('parts', []))
                session.record('prompt', text=message[:20000])
                if message.startswith('/computer-use'):
                    task = message[len('/computer-use') :].strip()
                    if task:
                        message = 'Use the installed computer-use tools to carry out this desktop task:\n' + task

                async def submit():
                    try:
                        # Preflight may itself request UI confirmation. The WebUI event
                        # consumer must run while Agent waits for that response.
                        result = await session.command('prompt', message=message, timeout=1800)
                        if (result or {}).get('disposition') == 'handled':
                            await session.finish()
                    except Exception as error:
                        await session.close()
                        session.busy = False
                        session.before = None
                        await emit(
                            directory,
                            'session.error',
                            session.id,
                            error=str(error) or 'Agent could not start the task.',
                        )

                task = asyncio.create_task(submit())
                session.tasks.append(task)
                task.add_done_callback(
                    lambda completed: session.tasks.remove(completed) if completed in session.tasks else None
                )
            except Exception:
                session.busy = False
                session.before = None
                raise
            return {'accepted': True}
    if action == 'abort':
        maintenance = session.maintenance_task
        if maintenance and not maintenance.done():
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
            session.record('aborted')
            return True
        if not session.process or session.process.returncode is not None:
            return True
        await session.dismiss_dialogs()
        await session.command('clear_queue')
        if session.compacting:
            await session.close()
            session.record('aborted')
            return True
        await session.command('abort', timeout=15)
        session.queue = {'steering': [], 'followUp': []}
        session.record('aborted')
        return True
    if action == 'ui':
        await session.answer(payload)
        return True
    if action == 'todo':
        return session.saved['todos']
    if action == 'diff':
        turns = session.saved['turns']
        turn = next(
            (
                turn
                for turn in reversed(turns)
                if not query.get('messageID') or turn['message_id'] == query['messageID']
            ),
            None,
        )
        return turn['diffs'] if turn else []
    if action in {'revert', 'unrevert'}:
        if any(item.busy and item.directory == directory for item in _sessions.values()):
            raise RuntimeError('Stop the Agent task before reverting files.')
        turns = session.saved['turns']
        turn = next(
            (
                turn
                for turn in reversed(turns)
                if turn['message_id']
                == (payload.get('messageID') if action == 'revert' else session.saved.get('reverted'))
            ),
            None,
        )
        if not turn:
            raise RuntimeError('No file snapshot is available for this response.')
        if action == 'revert' and session.saved.get('reverted'):
            raise RuntimeError('Restore the existing reverted response before reverting another one.')
        expected, target = (turn['after'], turn['before']) if action == 'revert' else (turn['before'], turn['after'])
        # Preflight every path before modifying any file, preserving unrelated edits.
        for name, content in expected.items():
            file = _safe_path(directory, name)
            actual = base64.b64encode(file.read_bytes()).decode() if file.is_file() else None
            if actual != content:
                raise RuntimeError(f'Cannot revert: {name} changed after this task.')
        for name, content in target.items():
            file = _safe_path(directory, name)
            if content is None:
                file.unlink(missing_ok=True)
            else:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(base64.b64decode(content))
        session.saved['reverted'] = turn['message_id'] if action == 'revert' else None
        session.save()
        return True
    raise RuntimeError(f'Unsupported Agent session action: {action}')


async def shutdown():
    for session in list(_sessions.values()):
        await session.close()
    _sessions.clear()
