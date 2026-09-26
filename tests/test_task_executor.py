"""Offline regression tests: no desktop actions, providers, or user data touched."""
import ast
import asyncio
import importlib.util
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import Mock, patch
from dataclasses import dataclass

ROOT = Path(__file__).resolve().parents[1]
@dataclass
class Message:
    role: str
    content: str

# Load the executor with inert dependencies, so tests cannot activate the desktop.
stubs = {
    'ai.base_provider': types.SimpleNamespace(Message=Message),
    'screen.capture': types.SimpleNamespace(capture_all_screens=lambda: []),
    'tutor': types.SimpleNamespace(active_window_title=lambda: 'Browser', is_sensitive_window=lambda _: False),
    'task_history': types.SimpleNamespace(task_history=Mock()),
    'ui.panel': types.SimpleNamespace(AppState=types.SimpleNamespace(THINKING='thinking', IDLE='idle')),
}
spec = importlib.util.spec_from_file_location('executor_under_test', ROOT / 'task_executor.py')
mod = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, stubs):
    spec.loader.exec_module(mod)


class Provider:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
    async def stream_response(self, **kwargs):
        self.calls.append(kwargs)
        reply = next(self.replies)
        if isinstance(reply, list):
            for part in reply:
                if isinstance(part, Exception):
                    raise part
                yield part
        else:
            yield reply


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.deps = patch.dict(sys.modules, stubs)
        self.deps.start()
        self.addCleanup(self.deps.stop)
        self.actions = []
        self.mgr = types.SimpleNamespace(
            _cancel_flag=False, _current_model='fake', _privacy_guard=False,
            _emit_state=Mock(), sig_response_chunk=Mock(), sig_response_done=Mock(),
            sig_status_text=Mock(), sig_clear_drawings=Mock(),
            _build_system_prompt_for_step=lambda **kw: '',
        )
        for method in ('_click_action', '_click_coord_action', '_type_action_text', '_key_action', '_execute_action_cmd'):
            setattr(self.mgr, method, lambda *args, m=method: self.actions.append((m, args)))
        self.ex = mod.TaskExecutor(self.mgr)
        async def no_wait(seconds):
            pass
        self.ex._wait = no_wait
        self.capture = patch.object(mod, 'capture_all_screens', return_value=[types.SimpleNamespace(base64_jpeg='screen')])
        self.capture.start()
        self.addCleanup(self.capture.stop)

    async def run_task(self, replies, goal='Find three remote jobs in Paris', context=None):
        self.provider = Provider(replies)
        self.mgr._get_llm = lambda: self.provider
        return await self.ex.execute_task(goal, context)

    async def test_progress_streams_before_actions_execute(self):
        waiting = asyncio.Event()
        release = asyncio.Event()
        async def stream(**kwargs):
            yield 'Checking the screen. [TYPE:hello]'
            waiting.set()
            await release.wait()
            yield ' Next step.'
        self.mgr._get_llm = lambda: types.SimpleNamespace(stream_response=stream)
        pending = asyncio.create_task(self.ex._response('', [], [], ''))
        await asyncio.wait_for(waiting.wait(), 1)
        self.assertFalse(pending.done())
        visible = ''.join(call.args[0] for call in self.mgr.sig_response_chunk.emit.call_args_list)
        self.assertIn('Checking the screen.', visible)
        self.assertNotIn('[TYPE:', visible)
        self.assertEqual(self.actions, [])
        release.set()
        await pending

    async def test_long_loading_preserves_goal_and_context(self):
        context = [Message('user', 'Salary must exceed 40000')]
        result = await self.run_task(['[WAIT:.2]'] * 12 + ['Found all three. [DONE]'], context=context)
        self.assertIn('completed', result)
        self.assertEqual(len(self.provider.calls), 13)
        for call in self.provider.calls:
            self.assertIn('Find three remote jobs in Paris', call['user_text'])
            self.assertEqual(call['history'][0].content, context[0].content)

    async def test_actions_order_repeated_keys_and_verification(self):
        await self.run_task(['[CLICK:100,200][TYPE:hello][KEY:Tab][KEY:Tab][DONE]', '[DONE]'])
        self.assertEqual([a[0] for a in self.actions], ['_click_coord_action', '_type_action_text', '_key_action', '_key_action'])
        self.assertEqual(len(self.provider.calls), 2)

    async def test_retry_never_executes_partial_stream(self):
        await self.run_task([['[CLICK:100,200]', RuntimeError('network')], '[CLICK:300,400]', '[DONE]'])
        self.assertEqual(self.actions, [('_click_coord_action', (300.0, 400.0))])

    async def test_resume_keeps_original_goal_and_plan(self):
        await self.run_task(['[PLAN:Find and compare three jobs][PROGRESS:Found first job][BLOCKED:Login needed]'])
        self.assertEqual(self.mgr._task_session['status'], 'blocked')
        await self.run_task(['[DONE]'], goal='continue')
        prompt = self.provider.calls[0]['user_text']
        self.assertIn('Find three remote jobs in Paris', prompt)
        self.assertIn('Find and compare three jobs', prompt)
        self.assertIn('Found first job', prompt)

    async def test_cancel_prevents_actions_and_success(self):
        async def response(*args):
            self.mgr._cancel_flag = True
            return '[CLICK:100,200][DONE]'
        self.ex._response = response
        result = await self.run_task([])
        self.assertIn('cancelled', result)
        self.assertEqual(self.actions, [])

    async def test_hung_stream_can_be_cancelled(self):
        async def stream(**kwargs):
            await asyncio.sleep(30)
            yield '[DONE]'
        self.mgr._get_llm = lambda: types.SimpleNamespace(stream_response=stream)
        async def cancel():
            await asyncio.sleep(.01)
            self.mgr._cancel_flag = True
        pending = asyncio.create_task(cancel())
        result = await asyncio.wait_for(self.ex._response('', [], [], ''), 1)
        await pending
        self.assertEqual(result, '')

    async def test_no_action_is_not_success(self):
        result = await self.run_task(['I will do it.'] * 3)
        self.assertIn('blocked', result)
        self.assertEqual(self.actions, [])

    async def test_budget_keeps_resumable_state(self):
        self.ex.MAX_STEPS = 2
        result = await self.run_task(['[WAIT:1]'] * 2)
        self.assertIn('paused', result)
        self.assertEqual(len(self.mgr._task_session['steps']), 2)

    async def test_new_goal_replaces_old_session(self):
        await self.run_task(['[BLOCKED:Login needed]'])
        await self.run_task(['[DONE]'], goal='Open calculator')
        self.assertEqual(self.mgr._task_session['goal'], 'Open calculator')

    async def test_unchanged_repeated_clicks_stop(self):
        result = await self.run_task(['[CLICK:100,200]'] * 3)
        self.assertIn('blocked', result)
        self.assertEqual(len(self.actions), 2)

    def test_followup_and_polite_request_routing(self):
        tree = ast.parse((ROOT / 'companion_manager.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_is_action_task')
        ns = {'re': re}
        with patch.dict(sys.modules, {'task_executor': mod}):
            exec(compile(ast.Module(body=[fn], type_ignores=[]), '<routing>', 'exec'), ns)
            for text in ('do the job', 'keep going', 'continue', 'can you find three jobs', 'please open firefox'):
                self.assertTrue(ns['_is_action_task'](text), text)
            self.assertFalse(ns['_is_action_task']('what is a web designer?'))

if __name__ == '__main__':
    unittest.main()
