import asyncio
import ast
import types
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock,patch
import numpy as np
from audio import playback
from speech_policy import external_speech_command


class AudioControls(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        playback.stop_audio()

    async def test_stop_cancels_a_stalled_request(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'companion_manager.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='CompanionManager')
        functions=[n for n in cls.body if getattr(n,'name','') in ('process_query','stop')]
        ns={'asyncio':asyncio,'is_stop':lambda text:False,
            'AppState':types.SimpleNamespace(IDLE='idle',LISTENING='listening')}
        exec(compile(ast.Module(body=functions,type_ignores=[]),'<manager>','exec'),ns)
        started=asyncio.Event()
        async def work(*args):
            started.set()
            await asyncio.sleep(30)
        mgr=types.SimpleNamespace(_process_query_impl=work,_cancel_narration=lambda:None,
            _state='thinking',_tts=None,_loop=asyncio.get_running_loop(),
            sig_status_text=Mock(),_emit_state=Mock())
        task=asyncio.create_task(ns['process_query'](mgr,'test request'))
        await started.wait()
        ns['stop'](mgr)
        await asyncio.wait_for(task,1)
        self.assertTrue(mgr._cancel_flag)
        self.assertFalse(mgr._query_running)
        self.assertIsNone(mgr._current_task)

    async def test_mute_aborts_actual_output_stream(self):
        playback.begin_audio_session()
        stream=Mock()
        playback._active_stream=stream
        playback.stop_audio()
        stream.abort.assert_called_once()
        self.assertTrue(playback._stop_event.is_set())
        playback._active_stream=None

    async def test_delayed_decoding_cannot_restart_after_mute(self):
        playback.begin_audio_session()
        decoding=threading.Event();release=threading.Event()
        def decode(data):
            decoding.set();release.wait(2)
            return np.zeros(100,dtype=np.float32),16000
        with patch.object(playback,'decode_mp3_to_pcm',side_effect=decode),patch.object(playback,'_blocking_play_chunked') as play:
            job=asyncio.create_task(playback.play_mp3_async(b'audio'))
            await asyncio.to_thread(decoding.wait,1)
            playback.stop_audio()
            playback.begin_audio_session() # even a new request cannot revive old audio
            release.set()
            await job
            play.assert_not_called()

    async def test_muted_audio_does_not_arm_itself(self):
        playback.stop_audio()
        with patch.object(playback,'decode_mp3_to_pcm') as decode:
            await playback.play_mp3_async(b'audio')
            decode.assert_not_called()

    async def test_sentences_share_stream_and_preserve_sample_order(self):
        playback.begin_audio_session()
        stream=Mock(active=True)
        with patch.object(playback.sd,'OutputStream',return_value=stream) as create:
            first=asyncio.create_task(playback.play_pcm_async(np.full(100,.2,dtype=np.float32),48000,drain=False))
            await asyncio.sleep(.02)
            second=asyncio.create_task(playback.play_pcm_async(np.full(100,.3,dtype=np.float32),48000,drain=False))
            await asyncio.sleep(.02)
            out=np.zeros((200,1),dtype=np.float32)
            create.call_args.kwargs['callback'](out,200,types.SimpleNamespace(outputBufferDacTime=1.),types.SimpleNamespace(output_underflow=False))
            await asyncio.wait_for(asyncio.gather(first,second),.5)
            create.assert_called_once()
            stream.start.assert_called_once()
            np.testing.assert_allclose(out[:100],.2)
            np.testing.assert_allclose(out[100:],.3)

    async def test_mute_releases_pending_audio_without_replay(self):
        playback.begin_audio_session()
        stream=Mock(active=True)
        with patch.object(playback.sd,'OutputStream',return_value=stream),patch.object(playback.sd,'play') as fallback:
            playing=asyncio.create_task(playback.play_pcm_async(np.ones(1000,dtype=np.float32),48000,drain=False))
            await asyncio.sleep(.02)
            playback.stop_audio()
            await asyncio.wait_for(playing,.3)
            stream.abort.assert_called_once()
            fallback.assert_not_called()

    async def test_final_buffer_drains_before_completion(self):
        playback.begin_audio_session()
        stream=Mock(active=True,time=1.)
        with patch.object(playback.sd,'OutputStream',return_value=stream):
            player=playback._PCMPlayer(48000,playback._epoch)
            player.last_sample_time=1.12
            playback._active_player=player
            playback._active_stream=stream
            draining=asyncio.create_task(playback.drain_audio_async())
            await asyncio.sleep(.03)
            self.assertFalse(draining.done())
            stream.time=1.13
            await asyncio.wait_for(draining,.2)

    def test_external_voice_commands_are_blocked(self):
        for command in ['spd-say "story"','espeak -v en "story"', 'bash -c \'spd-say "story"\'', 'python -c "import pyttsx3"']:
            self.assertTrue(external_speech_command(command),command)
        for command in ['google-chrome https://example.com','nemo ~/Downloads','echo "hello"']:
            self.assertFalse(external_speech_command(command),command)

if __name__=='__main__':unittest.main()
