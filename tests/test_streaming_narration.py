import asyncio
import unittest
from audio.streaming_narration import SentenceBuffer,StreamingNarrator,clean_text
from response_policy import needs_web_search,settle_delay


class BufferTests(unittest.TestCase):
    def test_list_numbers_and_abbreviations_stay_with_their_sentence(self):
        b=SentenceBuffer()
        self.assertEqual(b.feed('1. '),[])
        self.assertEqual(b.feed('Ask Dr. Rossi about the course. '),['1. Ask Dr. Rossi about the course. '])

    def test_split_tags_are_not_spoken(self):
        b=SentenceBuffer()
        self.assertEqual(b.feed('[TYPE:Hello. '),[])
        self.assertEqual(b.feed('world][CLICK:100,200]I found it. '),['[TYPE:Hello. world][CLICK:100,200]I found it. '])
        self.assertEqual(b.feed('Last sentence',final=True),['Last sentence'])

    def test_long_commands_and_unbroken_words_never_split_inside_tags(self):
        b=SentenceBuffer()
        self.assertEqual(b.feed('[TYPE:'+'x'*500+']'),[])
        self.assertEqual(b.feed('',final=True),[])
        b=SentenceBuffer()
        self.assertEqual(b.feed('x'*300),[])
        self.assertEqual(b.feed('',final=True),['x'*300])

    def test_final_drawing_preserved_and_truncated_tag_discarded(self):
        b=SentenceBuffer()
        self.assertEqual(b.feed('[CIRCLE:10,20,30]',final=True),['[CIRCLE:10,20,30]'])
        b=SentenceBuffer()
        self.assertEqual(b.feed('Hello [TYPE:secret',final=True),['Hello '])

    def test_search_is_skipped_only_for_explicit_local_requests(self):
        for text in ['hello','what is on my screen?','where is the save button?']:
            self.assertFalse(needs_web_search(text),text)
        for text in ['latest news','research this company','is this price on my screen a good deal?',
                     'who is the CEO on my screen?', 'verify what is on my screen']:
            self.assertTrue(needs_web_search(text),text)

    def test_wait_not_counted_twice_but_navigation_still_settles(self):
        self.assertEqual(settle_delay('[CLICK:10,20][WAIT:2]'),0)
        self.assertEqual(settle_delay('[WAIT:2][CLICK:10,20]'),1.5)
        self.assertEqual(settle_delay('[TYPE:hello]'),.15)


class NarrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_speech_starts_before_generation_finishes_and_prefetches(self):
        spoken=[];prepared=[]
        first_audio=asyncio.Event();release_audio=asyncio.Event();second_ready=asyncio.Event()
        async def prepare(raw):
            prepared.append(clean_text(raw).strip())
            if len(prepared)==2: second_ready.set()
            return raw.encode()
        async def speak(raw,payload):
            spoken.append(clean_text(raw).strip())
            if len(spoken)==1:
                first_audio.set()
                await release_audio.wait()
        n=StreamingNarrator(prepare,speak)
        n.feed('First sentence. ')
        await asyncio.wait_for(first_audio.wait(),1)
        self.assertFalse(n.closed)  # generation has not ended
        n.feed('Second sentence. ')
        await asyncio.wait_for(second_ready.wait(),1)
        self.assertEqual(spoken,['First sentence.']) # next audio prepared during playback
        release_audio.set()
        n.feed('Final sentence without punctuation')
        await asyncio.wait_for(n.finish(),1)
        self.assertEqual(spoken,['First sentence.','Second sentence.','Final sentence without punctuation'])

    async def test_cancel_discards_queued_speech(self):
        speaking=asyncio.Event();spoken=[]
        async def prepare(raw):return raw
        async def speak(raw,payload):
            spoken.append(raw);speaking.set();await asyncio.sleep(30)
        n=StreamingNarrator(prepare,speak)
        n.feed('One. Two. Three. ')
        await asyncio.wait_for(speaking.wait(),1)
        await asyncio.wait_for(n.abort(),1)
        self.assertEqual(len(spoken),1)
        n.feed('Do not speak this. ')
        self.assertTrue(n.cancelled)

    async def test_synthesis_error_does_not_hang_text_completion(self):
        errors=[]
        async def prepare(raw):raise RuntimeError('service unavailable')
        async def speak(raw,payload):self.fail('No audio should play')
        n=StreamingNarrator(prepare,speak,errors.append)
        n.feed('Hello. ')
        await asyncio.wait_for(n.finish(),1)
        self.assertEqual(len(errors),1)

if __name__=='__main__':unittest.main()
