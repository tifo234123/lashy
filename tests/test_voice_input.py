import ast
import asyncio
from pathlib import Path
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

def manager_method(name):
    tree=ast.parse((ROOT/'companion_manager.py').read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='CompanionManager')
    fn=next(n for n in cls.body if getattr(n,'name','')==name)
    ns={'asyncio':asyncio,'time':time,'Optional':str,'AppState':types.SimpleNamespace(LISTENING='listening')}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'<manager>','exec'),ns)
    return ns[name]

class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_holding_does_not_end_on_silence(self):
        calls=[]
        async def finish(): calls.append('done')
        mgr=types.SimpleNamespace(_capture_id=1,_state='listening',_ptt_held=True,_last_rms=0,
            _SPEECH_RMS=.006,_MAX_UTTERANCE_S=.18,_NO_SPEECH_S=.01,_END_SILENCE_S=.01,
            _claim_end=lambda:True,_end_capture_and_process=finish)
        start=time.monotonic()
        await manager_method('_auto_stop_on_silence')(mgr,1)
        self.assertGreaterEqual(time.monotonic()-start,.18)
        self.assertEqual(calls,['done'])

    async def test_old_watchdog_cannot_stop_new_capture(self):
        calls=[]
        async def finish(): calls.append('done')
        mgr=types.SimpleNamespace(_capture_id=2,_state='listening',_end_capture_and_process=finish)
        await manager_method('_auto_stop_on_silence')(mgr,1)
        self.assertEqual(calls,[])

    def test_microphone_failure_surfaces_and_resets_mode(self):
        from audio.ambient_listener import AmbientListener,Mode
        listener=AmbientListener(lambda _:None,lambda:None,ambient=False)
        with patch('audio.ambient_listener.sd.InputStream',side_effect=RuntimeError('unavailable')),patch('audio.ambient_listener.sd.query_devices',side_effect=RuntimeError('unavailable')):
            with self.assertRaisesRegex(RuntimeError,'Microphone unavailable'):
                listener.start_recording()
        self.assertIsNone(listener._stream)
        self.assertEqual(listener._mode,Mode.STANDBY)

if __name__=='__main__': unittest.main()
