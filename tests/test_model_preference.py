"""Model preference persistence without reading or modifying user settings."""
import ast
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def methods(path, class_name, names):
    tree = ast.parse((ROOT / path).read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    return ast.Module(body=[n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])


class Combo:
    def __init__(self):
        self.items = []; self.index = -1
    def currentData(self):
        return self.items[self.index][1] if self.index >= 0 else None
    def blockSignals(self, value): pass
    def clear(self): self.items = []; self.index = -1
    def addItem(self, label, value): self.items.append((label, value))
    def findData(self, value):
        return next((i for i, pair in enumerate(self.items) if pair[1] == value), -1)
    def setCurrentIndex(self, index): self.index = index


class ModelPreferenceTests(unittest.TestCase):
    def test_selection_survives_environment_reload(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / '.env'
            ns = {'os': os}
            exec(compile(methods('config.py', 'Config', {'selected_model', 'set_selected_model', '_write_env'}), '<config>', 'exec'), ns)
            cls = type('Config', (), {k:v for k,v in ns.items() if callable(v)})
            cfg = cls(); cfg.env_path = lambda: path
            cfg.set_selected_model('gemini-choice', 'gemini')
            cfg.set_selected_model('claude-choice', 'claude')
            cfg.set_selected_model('gemini-latest', 'gemini')
            os.environ.clear()
            for line in path.read_text().splitlines():
                key, value = line.split('=', 1); os.environ[key] = value
            restored = cls()
            self.assertEqual(restored.selected_model('gemini'), 'gemini-latest')
            self.assertEqual(restored.selected_model('claude'), 'claude-choice')

    def test_refresh_and_provider_switch_restore_selection(self):
        saved = {'gemini': 'gemini-saved', 'claude': 'claude-saved'}
        cfg = types.SimpleNamespace(selected_model=lambda provider: saved.get(provider, ''))
        ns = {'cfg': cfg, 'DEMO': False}
        exec(compile(methods('ui/panel.py', 'CompanionPanel', {'_set_models_for'}), '<panel>', 'exec'), ns)
        panel = types.SimpleNamespace(_model_combo=Combo(), _emit_model=lambda: None)
        registry = types.SimpleNamespace(cached_models=lambda provider: [{'id':provider+'-default','vision':True}])
        with patch.dict('sys.modules', {'ai.model_registry': registry}):
            for provider in ('gemini', 'claude', 'gemini'):
                ns['_set_models_for'](panel, provider)
                self.assertEqual(panel._model_combo.currentData(), saved[provider])
            ns['_set_models_for'](panel, 'gemini')
            self.assertEqual(panel._model_combo.currentData(), 'gemini-saved')

if __name__ == '__main__':
    unittest.main()
