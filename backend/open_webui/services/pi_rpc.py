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
_IGNORED = {'.git', 'node_modules', '.venv', '__pycache__', '.pi', '.tmp', 'dist', '.svelte-kit'}


def descriptor() -> dict:
    file = os.getenv('AURAPRO_PI_RUNTIME_FILE') or os.getenv('AURAPRO_OPENCODE_RUNTIME_FILE', '')
    if not file:
        raise RuntimeError('PI is not configured. Install and start PI in Desktop settings.')
    data = json.loads(Path(file).read_text(encoding='utf-8'))
    if data.get('engine') != 'pi' or data.get('version') != 2:
        raise RuntimeError('The PI runtime descriptor is invalid. Restart PI in Desktop settings.')
    if not Path(data['executable']).is_file():
        raise RuntimeError('The installed PI executable is missing.')
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
            raise RuntimeError('Desktop is no longer running. Restart PI from Desktop settings.') from error
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
    for parent, directories, files in os.walk(directory, followlinks=False):
        directories[:] = [
            name for name in directories if name not in _IGNORED and not (Path(parent) / name).is_symlink()
        ]
        for name in files:
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
        old = base64.b64decode(before.get(name, ''))
        new = base64.b64decode(after.get(name, ''))
        patch = ''
        try:
            old_text, new_text = old.decode('utf-8'), new.decode('utf-8')
            if '\x00' not in old_text + new_text:
                patch = ''.join(
                    difflib.unified_diff(
                        old_text.splitlines(True), new_text.splitlines(True), fromfile=f'a/{name}', tofile=f'b/{name}'
                    )
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
        self.interactive = interactive
        self.mode = 'build'
        self.ui: dict[str, dict] = {}
        self.before: dict | None = None
        self.skipped: list[str] = []
        self.prompt_lock = _workspace_locks.setdefault(directory, asyncio.Lock())

    def save(self):
        temporary = self.file.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.saved), encoding='utf-8')
        temporary.replace(self.file)

    async def start(self, mode='build'):
        if self.process and self.process.returncode is None:
            if mode == self.mode:
                return
            if self.busy:
                raise RuntimeError('Cannot change mode while PI is running.')
            await self.close()
        self.mode = mode
        env = {
            **os.environ,
            **self.data.get('environment', {}),
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
            args += ['--tools', 'read,grep,find,ls']
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
        await self.command('get_state', timeout=30)
        if not self.diagnostics.get('auraproDiagnostics'):
            await self.close()
            raise RuntimeError(
                'The AuraPro PI bridge did not load. Restart PI in Desktop settings and check extension installation.'
            )

    async def watch_runtime(self):
        while self.process and self.process.returncode is None:
            await asyncio.sleep(2)
            try:
                current = descriptor()
                if current != self.data:
                    raise RuntimeError('PI runtime settings changed. Start a new turn.')
            except Exception:
                await self.close()
                return

    async def drain_errors(self):
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            self.saved['last_error'] = line.decode('utf-8', errors='replace').strip()[:2000]

    async def send(self, value: dict):
        if not self.process or self.process.returncode is not None or not self.process.stdin:
            raise RuntimeError('PI process is not running.')
        self.process.stdin.write(json.dumps(value, ensure_ascii=False).encode('utf-8') + b'\n')
        await self.process.stdin.drain()

    async def command(self, kind: str, timeout=30, **values: Any):
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({'id': request_id, 'type': kind, **values})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def read(self):
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                event = json.loads(line)
                kind = event.get('type')
                if kind == 'response':
                    future = self.pending.get(event.get('id'))
                    if future and not future.done():
                        if event.get('success'):
                            future.set_result(event.get('data'))
                        else:
                            future.set_exception(RuntimeError(str(event.get('error') or 'PI command failed.')))
                elif kind == 'extension_ui_request':
                    try:
                        diagnostic = json.loads(event.get('message') or '')
                    except (ValueError, TypeError):
                        diagnostic = {}
                    if diagnostic.get('auraproDiagnostics'):
                        self.diagnostics = diagnostic
                    elif diagnostic.get('auraproProbe'):
                        self.probe = diagnostic
                    else:
                        if event.get('method') in {'confirm', 'select', 'input', 'editor'} and (
                            not self.interactive or not _subscribers.get(self.directory)
                        ):
                            await self.send({'type': 'extension_ui_response', 'id': event['id'], 'cancelled': True})
                        else:
                            self.ui[event['id']] = event
                            await emit(self.directory, 'pi.ui', self.id, request=event)
                elif kind == 'agent_start':
                    self.busy = True
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
                    self.live_text = ''
                    self.live_id = ''
                    if event.get('message', {}).get('stopReason') == 'error':
                        await emit(
                            self.directory,
                            'session.error',
                            self.id,
                            error=event['message'].get('errorMessage') or 'PI model request failed.',
                        )
                    else:
                        await emit(self.directory, 'message.part.updated', self.id)
                elif kind == 'agent_settled':
                    # The RPC reader must stay free to receive the get_messages response.
                    asyncio.create_task(self.finish())
                else:
                    await emit(self.directory, 'message.part.updated', self.id)
        except (ValueError, OSError) as error:
            self.saved['last_error'] = str(error)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError(self.saved.get('last_error') or 'PI process exited.'))
            if self.busy:
                self.busy = False
                await emit(
                    self.directory,
                    'session.error',
                    self.id,
                    error=self.saved.get('last_error') or 'PI process exited before completion.',
                )

    async def finish(self):
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
            await emit(self.directory, 'session.idle', self.id)
        except Exception as error:
            self.busy = False
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
                result.append({'info': {'id': message_id, 'role': role, 'parentID': parent}, 'parts': parts})
            elif role == 'toolResult' and message.get('toolCallId') in tools:
                tools[message['toolCallId']]['state'].update(
                    {
                        'status': 'error' if message.get('isError') else 'completed',
                        'output': message.get('content'),
                        'metadata': message.get('details', {}),
                    }
                )
        if self.live_text:
            result.append(
                {
                    'info': {'id': self.live_id, 'role': 'assistant', 'parentID': parent},
                    'parts': [{'type': 'text', 'text': self.live_text}],
                }
            )
        return result

    async def close(self):
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
        for task in self.tasks:
            if task is not current:
                task.cancel()
        self.process = None


async def get_session(session_id: str, directory: str) -> PiSession:
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
            raise RuntimeError('PI session belongs to a different workspace.')
        current = descriptor()
        if current != session.data:
            await session.close()
            session.data = current
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
                'description': 'Read-only workspace analysis' if mode == 'plan' else 'PI with installed extensions',
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
        raise RuntimeError(f'Unsupported PI endpoint: {endpoint}')
    session = await get_session(parts[1], directory)
    action = parts[2] if len(parts) > 2 else ''
    if not action:
        if method == 'DELETE':
            await session.close()
            _sessions.pop(session.id, None)
        return {'id': session.id}
    if action == 'message':
        return await session.messages()
    if action == 'prompt_async':
        async with session.prompt_lock:
            if any(item.busy and item.directory == directory for item in _sessions.values()):
                raise RuntimeError('A PI task is already running in this workspace. Wait or stop it first.')
            if session.saved.get('reverted'):
                raise RuntimeError(
                    'Restore the reverted files or reset this conversation before starting another task.'
                )
            await session.start(payload.get('agent', 'build'))
            model = payload.get('model')
            if model:
                await session.command('set_model', provider=model['providerID'], modelId=model['modelID'])
            session.busy = True
            try:
                session.before, session.skipped = await asyncio.to_thread(snapshot, directory)
                result = await session.command(
                    'prompt', message='\n'.join(part.get('text', '') for part in payload.get('parts', []))
                )
                if (result or {}).get('disposition') == 'handled':
                    await session.finish()
            except Exception:
                session.busy = False
                session.before = None
                raise
            return {'accepted': True}
    if action == 'abort':
        await session.command('clear_queue')
        await session.command('abort', timeout=15)
        return True
    if action == 'ui':
        ui_id = payload.get('id')
        event = session.ui.pop(ui_id, None)
        if not event:
            raise RuntimeError('This PI dialog expired.')
        await session.send({'type': 'extension_ui_response', **payload})
        return True
    if action == 'todo':
        return []
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
            raise RuntimeError('Stop the PI task before reverting files.')
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
    raise RuntimeError(f'Unsupported PI session action: {action}')


async def shutdown():
    for session in list(_sessions.values()):
        await session.close()
    _sessions.clear()
