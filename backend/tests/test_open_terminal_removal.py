import ast
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / 'backend' / 'open_webui'


def source_tree(relative):
    return ast.parse((BACKEND / relative).read_text(encoding='utf-8'))


def identifiers_and_strings(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr
        elif isinstance(node, ast.alias):
            yield node.name
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


class OpenTerminalRemovalTests(unittest.TestCase):
    def assert_no_terminal_contract(self, tree):
        forbidden = re.compile('terminal', re.IGNORECASE)
        self.assertEqual([value for value in identifiers_and_strings(tree) if forbidden.search(value)], [])

    def test_main_has_no_terminal_router_or_startup(self):
        tree = source_tree('main.py')
        self.assert_no_terminal_contract(tree)
        routers = [
            node.args[0].value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'include_router'
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and isinstance(node.args[0].value, ast.Name)
        ]
        self.assertNotIn('terminals', routers)
        self.assertIn('configs', routers)
        self.assertFalse((BACKEND / 'routers' / 'terminals.py').exists())

    def test_no_terminal_config_or_events(self):
        for relative in ('config.py', 'routers/configs.py', 'events.py'):
            with self.subTest(relative=relative):
                self.assert_no_terminal_contract(source_tree(relative))
        values = set(identifiers_and_strings(source_tree('routers/configs.py')))
        self.assertIn('code_interpreter.engine', values)
        self.assertIn('/tool_servers', values)

    def test_automation_schema_has_only_supported_fields(self):
        tree = source_tree('models/automations.py')
        self.assert_no_terminal_contract(tree)
        schema = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'AutomationData')
        fields = {node.target.id for node in schema.body if isinstance(node, ast.AnnAssign)}
        self.assertEqual(fields, {'prompt', 'model_id', 'rrule'})

    def test_automation_and_tool_execution_have_no_terminal_dispatch(self):
        for relative in ('utils/automations.py', 'utils/tools.py', 'utils/middleware.py'):
            with self.subTest(relative=relative):
                self.assert_no_terminal_contract(source_tree(relative))
        values = set(identifiers_and_strings(source_tree('utils/tools.py')))
        self.assertIn('execute_code', values)
        self.assertIn('code_interpreter.enable', values)

    def test_no_frontend_imports_of_deleted_terminal_modules(self):
        deleted = {
            'apis/terminal',
            'AddTerminalServerModal.svelte',
            'XTerminal.svelte',
            'FileNav.svelte',
            'TerminalMenu.svelte',
            'Terminals.svelte',
            'Terminals/Connection.svelte',
            'TerminalSelector.svelte',
            'TerminalDropdown.svelte',
            'PortList.svelte',
            'PortPreview.svelte',
            'CellEditor.svelte',
        }
        imports = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s*)['"]([^'"\n]+)['"]""")
        for path in (ROOT / 'src').rglob('*'):
            if path.suffix not in ('.ts', '.svelte', '.js'):
                continue
            for module in imports.findall(path.read_text(encoding='utf-8')):
                with self.subTest(file=str(path.relative_to(ROOT)), module=module):
                    self.assertFalse(any(module == item or module.endswith('/' + item) for item in deleted))

    def test_shared_pyodide_file_imports_resolve(self):
        chat = ROOT / 'src/lib/components/chat'
        imports = re.compile(r"""\bfrom\s*['"]([^'"\n]+)['"]""")
        files = [chat / 'PyodideFileNav.svelte', *(chat / 'FileNav').glob('*.svelte')]
        checked = 0
        for path in files:
            for module in imports.findall(path.read_text(encoding='utf-8')):
                if not module.startswith('.'):
                    continue
                target = path.parent / module
                with self.subTest(file=path.name, module=module):
                    self.assertTrue(target.is_file() or target.with_suffix('.ts').is_file())
                checked += 1
        self.assertGreater(checked, 5)

    def test_pyodide_controls_worker_and_agent_execution_remain(self):
        controls = (ROOT / 'src/lib/components/chat/ChatControls.svelte').read_text(encoding='utf-8')
        self.assertEqual(controls.count('<PyodideFileNav'), 2)
        self.assertIn("$codeInterpreterEnabled && $config?.code?.interpreter_engine !== 'jupyter'", controls)
        worker = (ROOT / 'src/lib/workers/pyodide.worker.ts').read_text(encoding='utf-8')
        self.assertIn('self.pyodide.runPythonAsync(code)', worker)
        self.assertIn('create_subprocess_exec', set(identifiers_and_strings(source_tree('services/pi_rpc.py'))))

    def test_no_terminal_keys_in_any_locale(self):
        resources = list((ROOT / 'src/lib/i18n/locales').glob('*/translation.json'))
        self.assertGreater(len(resources), 4)
        for path in resources:
            messages = json.loads(path.read_text(encoding='utf-8'))
            with self.subTest(locale=path.parent.name):
                self.assertEqual([key for key in messages if 'terminal' in key.lower()], [])

    def test_automation_frontend_has_no_terminal_fields(self):
        files = [ROOT / 'src/lib/apis/automations/index.ts']
        files.extend((ROOT / 'src/lib/components/automations').glob('*.svelte'))
        files.extend((ROOT / 'src/routes/(app)/automations').rglob('*.svelte'))
        for path in files:
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertNotRegex(path.read_text(encoding='utf-8'), r'(?i)terminal')


if __name__ == '__main__':
    unittest.main()
