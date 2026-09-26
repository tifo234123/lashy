"""Sentence-level narration pipelined with generation and audio preparation."""
import asyncio
import re

TAGS = re.compile(r'\[(?:POINT|ARROW|CIRCLE|UNDERLINE|LABEL|LINE|RECT|POLY|TEXT|ANGLE|CLEAR|EXEC|TYPE|KEY|CLICK|WAIT|DONE|STEP|PLAN|PROGRESS|BLOCKED)(?::[^\]]*)?\]')
PARTIAL = re.compile(r'\[[A-Z]{0,12}(?::[^\]]*)?$')


def clean_text(text):
    return TAGS.sub('', text)


class SentenceBuffer:
    def __init__(self):
        self.pending = ''

    def feed(self, chunk, final=False):
        self.pending += chunk
        result = []
        while self.pending:
            # Mask tags without changing offsets. Punctuation inside an action
            # must never be interpreted as the end of a spoken sentence.
            masked = TAGS.sub(lambda m: '~' * len(m.group()), self.pending)
            partial = PARTIAL.search(masked)
            boundary_limit = partial.start() if partial else len(masked)
            visible = masked[:boundary_limit]
            match = None
            for candidate in re.finditer(r'[.!?][\"\u201d\u2019*]*(?:\s+)|\n\s*\n', visible):
                prefix = clean_text(self.pending[:candidate.end()]).strip()
                # Numbered bullets and abbreviations are not spoken sentences.
                if re.fullmatch(r'\d+\.', prefix) or re.search(r'\b(?:Dr|Mr|Mrs|Ms|Prof|e\.g|i\.e|vs)\.$', prefix):
                    continue
                match = candidate
                break
            end = match.end() if match else 0
            if not end and len(visible) > 260:
                end = max(0, visible.rfind(' ', 100, 260))
            if not end and final:
                end = boundary_limit
            if not end:
                break
            segment, self.pending = self.pending[:end], self.pending[end:]
            if clean_text(segment).strip() or re.search(r'\[(?:ARROW|CIRCLE|UNDERLINE|LABEL|LINE|RECT|POLY|TEXT|ANGLE):', segment):
                result.append(segment)
        if final:
            self.pending = ''  # never speak a truncated control tag
        return result


class StreamingNarrator:
    """One synthesis worker prefetches while one playback worker speaks in order.

    The prepared queue is bounded so speech does not trigger unbounded parallel
    provider calls. Text generation never waits for the length of the audio.
    """
    def __init__(self, prepare, speak, on_error=None, on_cancel=None, on_finish=None):
        self.buffer = SentenceBuffer()
        self.pending = asyncio.Queue()
        self.ready = asyncio.Queue(maxsize=2)
        self.prepare = prepare
        self.speak = speak
        self.on_error = on_error or (lambda exc: None)
        self.on_cancel = on_cancel or (lambda: None)
        self.on_finish = on_finish
        self.closed = False
        self.cancelled = False
        self._producer = asyncio.create_task(self._prepare())
        self._consumer = asyncio.create_task(self._play())

    def feed(self, chunk):
        if not self.closed:
            for segment in self.buffer.feed(chunk):
                self.pending.put_nowait(segment)

    def reset_partial(self):
        self.buffer.pending = ""

    async def _prepare(self):
        try:
            while True:
                raw = await self.pending.get()
                if raw is None:
                    await self.ready.put(None)
                    return
                payload = await self.prepare(raw)
                await self.ready.put((raw, payload))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.on_error(exc)
            self.cancel()

    async def _play(self):
        try:
            while True:
                item = await self.ready.get()
                if item is None:
                    return
                await self.speak(*item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.on_error(exc)
            self.cancel()

    async def finish(self):
        if not self.closed:
            for segment in self.buffer.feed('', final=True):
                self.pending.put_nowait(segment)
            self.closed = True
            self.pending.put_nowait(None)
        await asyncio.gather(self._producer, self._consumer, return_exceptions=True)
        if not self.cancelled and self.on_finish:
            await self.on_finish()

    def cancel(self):
        if not self.cancelled:
            self.on_cancel()
        self.closed = self.cancelled = True
        self.buffer.pending = ''
        for worker in (self._producer, self._consumer):
            worker.cancel()

    async def abort(self):
        self.cancel()
        await asyncio.gather(self._producer, self._consumer, return_exceptions=True)
