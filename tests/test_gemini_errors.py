import unittest
from unittest.mock import patch
import httpx
from ai.gemini_provider import GeminiProvider

class GeminiErrors(unittest.IsolatedAsyncioTestCase):
 async def test_explicit_model_does_not_silently_fall_back(self):
  provider=GeminiProvider()
  await provider._client.aclose()
  requested=[]
  def unavailable(request):
   requested.append(request.url.path)
   return httpx.Response(503,request=request)
  provider._client=httpx.AsyncClient(transport=httpx.MockTransport(unavailable))
  try:
   with self.assertLogs('clicky.gemini',level='WARNING'):
    with self.assertRaises(RuntimeError):
     async for _ in provider.stream_response('hello',[],[],'test',model='gemini-3.8-flash'):pass
   self.assertEqual(requested,['/v1beta/models/gemini-3.8-flash:streamGenerateContent'])
  finally:await provider.close()

 async def test_authenticated_url_is_not_exposed(self):
  provider=GeminiProvider()
  await provider._client.aclose()
  provider._api_key='private-test-key'
  provider._client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(503,request=request)))
  try:
   with self.assertLogs('clicky.gemini',level='WARNING') as log:
    with self.assertRaises(RuntimeError) as error:
     async for _ in provider.stream_response('hello',[],[],'test'):pass
   self.assertIn('temporarily unavailable',str(error.exception))
   self.assertNotIn('private-test-key',str(error.exception))
   self.assertNotIn('private-test-key','\n'.join(log.output))
  finally:await provider.close()
