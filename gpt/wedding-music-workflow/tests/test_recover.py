"""Offline behavioral checks for the bounded single-retry helper."""
import argparse
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import recover
import pipeline


def info(bitrate=320000):
    return {'source_format':'mp3', 'source_bitrate':bitrate, 'source_sample_rate':44100,
            'source_channels':2, 'source_bits_per_sample':0, 'source_duration':180,
            'source_is_mp3_320':bitrate==320000}


class Response:
    status_code=200
    headers={}
    def json(self): return {'data':'https://audio.invalid/source.mp3'}
    def raise_for_status(self): pass
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def iter_content(self,*args,**kwargs): yield b'fake-audio-fixture'


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.work=self.root/'batch'; self.work.mkdir()
        self.session=self.root/'session.json'
        self.record={'source':'QQMusicClient','metadata_only':True,'identifier':'fixed-mid','duration_s':180}
        self.session.write_text(json.dumps({'created':time.time(),'candidates':[{'id':7,'record':self.record}]}))
        self.job={'index':1,'stage':'开场','title':'Fixture','artists':['Artist'],'start':0,
                  'session':'session.json','candidate_id':7,'confirmed':True}
        self.manifest=self.root/'confirmed.json'
        self.manifest.write_text(json.dumps({'tracks':[self.job]}))
        pipeline.save(self.work/'report.json',{'results':[{'index':1,'status':'failed','job_digest':pipeline.job_digest(self.job)}]})
        self.args=argparse.Namespace(manifest=self.manifest,work=self.work,out=self.root/'retry',ids='1',allow_low_bitrate_ids='')
    def tearDown(self): self.temp.cleanup()

    def attempt(self, bitrate=320000, allow=False):
        folder=self.root/'audio'; folder.mkdir()
        transport=Mock(); transport.post.return_value=Response(); transport.get.return_value=Response()
        status={'ok':True,'probe_codec':'mp3','probe_bitrate':bitrate,'download_url':'https://audio.invalid/source.mp3',
                'file_size_bytes':18,'ext':'mp3'}
        with patch.object(recover.metadata,'_AudioProbe') as cls, patch.object(recover.music,'inspect_download',return_value=info(bitrate)):
            cls.return_value.test.return_value=status
            result=recover.attempt(self.record,folder,allow,transport)
        return result,transport

    def test_complete_download_and_fixed_id(self):
        result,transport=self.attempt()
        self.assertEqual(result['status'],'downloaded')
        self.assertTrue(Path(result['path']).is_file())
        self.assertEqual(transport.post.call_args.kwargs['data']['id'],'fixed-mid')
        transport.post.assert_called_once()
        self.assertNotIn('download_url',json.dumps(result))
        self.assertNotIn('https://',json.dumps(result))

    def test_low_quality_rejected_before_transfer(self):
        result,transport=self.attempt(128000)
        self.assertEqual(result['error_kind'],'quality_unavailable')
        self.assertEqual(result['available_bitrates'],[128000])
        transport.get.assert_not_called()

    def test_low_quality_explicitly_accepted_and_reported(self):
        result,_=self.attempt(128000,True)
        self.assertEqual(result['status'],'downloaded')
        self.assertEqual(result['quality']['source_bitrate'],128000)
        self.assertFalse(result['quality']['source_is_mp3_320'])

    def test_quality_changed_rejected(self):
        folder=self.root/'audio'; folder.mkdir()
        transport=Mock(); transport.post.return_value=Response(); transport.get.return_value=Response()
        with patch.object(recover.metadata,'_AudioProbe') as cls, patch.object(recover.music,'inspect_download',return_value=info(128000)):
            cls.return_value.test.return_value={'ok':True,'probe_codec':'mp3','probe_bitrate':320000,'ext':'mp3','download_url':'https://audio.invalid/x'}
            result=recover.attempt(self.record,folder,False,transport)
        self.assertEqual(result['error_kind'],'quality_changed')
        self.assertFalse(list(folder.iterdir()))

    def test_success_rewrites_manifest_and_preserves_job_metadata(self):
        def download(record,folder,allow):
            path=folder/'source.mp3'; path.write_bytes(b'audio')
            return {'status':'downloaded','path':str(path),'quality':info(),'source_sha256':pipeline.digest(path)}
        self.assertEqual(recover.recover(self.args,download),0)
        final=pipeline.load(self.args.out/'recover-manifest.json')['tracks'][0]
        self.assertEqual(final['title'],self.job['title'])
        self.assertEqual(final['start'],0)
        self.assertNotIn('session',final)
        self.assertTrue(Path(final['source_file']).is_file())

    def test_same_lock_as_pipeline(self):
        with pipeline.batch_lock(self.work):
            with self.assertRaisesRegex(ValueError, 'already in use'):
                recover.recover(self.args, lambda *args:self.fail('Concurrent attempt'))
        def check_locked(*args):
            with self.assertRaisesRegex(ValueError, 'already in use'):
                with pipeline.batch_lock(self.work):
                    self.fail('Concurrent pipeline')
            return {'status':'failed','error_kind':'quality_unavailable'}
        self.assertEqual(recover.recover(self.args, check_locked), 2)
        self.assertFalse((self.work/'.run.lock').exists())

    def test_repeat_blocked_even_with_new_output(self):
        recover.recover(self.args,lambda *args:{'status':'failed','error_kind':'quality_unavailable'})
        self.args.out=self.root/'second'
        with self.assertRaisesRegex(ValueError,'already recorded'):
            recover.recover(self.args,lambda *args:self.fail('Network attempted twice'))

    def test_expired_session_rejected_before_network(self):
        data=pipeline.load(self.session); data['created']=time.time()-86401; pipeline.save(self.session,data)
        with self.assertRaisesRegex(ValueError,'expired'):
            recover.recover(self.args,lambda *args:self.fail('Expired request'))

    def test_manifest_change_rejected(self):
        data=pipeline.load(self.manifest); data['tracks'][0]['candidate_id']=8; pipeline.save(self.manifest,data)
        with self.assertRaisesRegex(ValueError,'evidence'):
            recover.recover(self.args,lambda *args:self.fail('Wrong candidate'))

    def test_timeout_is_recorded_without_sensitive_message(self):
        def fail(*args): raise TimeoutError('https://secret.invalid/token')
        self.assertEqual(recover.recover(self.args,fail),2)
        report=(self.args.out/'recovery-report.json').read_text()
        self.assertIn('TimeoutError',report)
        self.assertNotIn('secret',report)

    def two_tracks(self):
        j=copy.deepcopy(self.job); j['index']=2
        pipeline.save(self.manifest,{'tracks':[self.job,j]})
        pipeline.save(self.work/'report.json',{'results':[{'index':t['index'],'status':'failed','job_digest':pipeline.job_digest(t)} for t in [self.job,j]]})
        self.args.ids='1,2'

    def test_timeout_does_not_cancel_later_track(self):
        self.two_tracks(); callback=Mock(side_effect=[TimeoutError(),{'status':'failed','error_kind':'quality_unavailable'}])
        self.assertEqual(recover.recover(self.args,callback),2)
        self.assertEqual(callback.call_count,2)

    def test_429_stops_queue_and_preserves_cooldown(self):
        self.two_tracks(); callback=Mock(side_effect=recover.RateLimited('120'))
        self.assertEqual(recover.recover(self.args,callback),2)
        callback.assert_called_once()
        results=pipeline.load(self.args.out/'recovery-report.json')['results']
        self.assertEqual(results[0]['retry_after_seconds'],120)
        self.assertEqual(results[0]['status'],'rate_limited')

    def test_transport_catches_429(self):
        response=Response(); response.status_code=429; response.headers={'Retry-After':'30'}
        backend=Mock(); backend.get.return_value=response
        with self.assertRaises(recover.RateLimited) as caught:
            recover.Transport(backend).get('https://audio.invalid')
        self.assertEqual(caught.exception.retry_after,30)

    def test_byte_budget_rejects_before_transfer(self):
        folder=self.root/'audio'; folder.mkdir(); transport=Mock(); transport.post.return_value=Response()
        with patch.object(recover.metadata,'_AudioProbe') as cls:
            cls.return_value.test.return_value={'ok':True,'probe_codec':'mp3','probe_bitrate':320000,'file_size_bytes':recover.MAX_BYTES+1}
            with self.assertRaisesRegex(ValueError,'size_limit'):
                recover.attempt(self.record,folder,False,transport)
        transport.get.assert_not_called()


if __name__=='__main__': unittest.main()
