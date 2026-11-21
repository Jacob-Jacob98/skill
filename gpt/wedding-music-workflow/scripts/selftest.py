"""Offline smoke test using a generated tone, never a user's music."""
import argparse
import array
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import wave

HERE = Path(__file__).resolve().parent

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--work',type=Path,required=True)
    a = p.parse_args()
    a.work.mkdir(parents=True,exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='music-skill-test-',dir=a.work.resolve()))
    source = root/'tone.wav'
    samples = array.array('h')
    for i in range(44100*4):
        value = int(10000*math.sin(2*math.pi*440*i/44100))
        samples.extend([value,value])
    if sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(str(source),'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100); w.writeframes(samples.tobytes())
    data = {'tracks':[
        dict(index=1,stage='开场',title='测试',artists=['甲/乙','丙\\丁'],start=0,confirmed=True,source_file='tone.wav'),
        dict(index=2,stage='交换戒指',title='裁剪测试',artists=['测试作者'],start='00:01',end=3,confirmed=True,source_file='tone.wav'),
        dict(index=3,stage='退场',title='起点裁剪',artists=['测试作者'],start='00:01',confirmed=True,source_file='tone.wav')
    ]}
    manifest = root/'manifest.json'
    def write():
        manifest.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    write()
    batch, final = root/'batch',root/'delivery'
    def call(action,extra=(),expected=0):
        result = subprocess.run([sys.executable,'-X','utf8',str(HERE/'pipeline.py'),action,'--manifest',str(manifest),'--work',str(batch),*extra],capture_output=True,text=True,encoding='utf-8')
        if result.returncode != expected:
            raise AssertionError(result.stdout+result.stderr)
    call('run')
    report = json.loads((batch/'report.json').read_text(encoding='utf-8'))['results']
    assert len(report)==3 and all(r['status']=='verified' for r in report)
    assert report[0]['file']=='1-开场《测试》甲 乙 丙 丁.mp3'
    assert report[0]['fade_in']==0 and report[1]['fade_in']==0.3 and report[2]['fade_in']==0.3
    assert abs(report[1]['duration']-2)<0.2 and abs(report[2]['duration']-3)<0.2
    # Decode cropped MP3 to PCM and check actual opening amplitude ramp.
    import imageio_ffmpeg
    raw = subprocess.check_output([imageio_ffmpeg.get_ffmpeg_exe(),'-v','error','-i',str(batch/'final'/report[1]['file']),'-f','f32le','-ac','1','-ar','44100','-'])
    floats = array.array('f'); floats.frombytes(raw)
    if sys.byteorder!='little':
        floats.byteswap()
    def rms(start,end):
        values = floats[int(start*44100):int(end*44100)]
        return math.sqrt(sum(x*x for x in values)/len(values))
    assert rms(0,0.05)<0.2*rms(0.5,0.55), 'Fade-in envelope not detected'
    call('run')
    resumed = json.loads((batch/'report.json').read_text(encoding='utf-8'))['results']
    assert all(r.get('resumed') for r in resumed), 'Verified outputs were not resumed'
    from pipeline import encode, digest
    copied = root/'copied.mp3'
    original_mp3 = batch/'final'/report[0]['file']
    copy_result = encode(original_mp3,copied,{'start':0},imageio_ffmpeg.get_ffmpeg_exe(),'balanced')
    assert copy_result['processing_mode']=='copy' and copy_result['full_decode']
    assert digest(original_mp3)==digest(copied), 'Compliant MP3 was reencoded'
    balanced = encode(source,root/'balanced.mp3',{'start':1,'end':3},imageio_ffmpeg.get_ffmpeg_exe(),'balanced')
    assert not balanced['full_decode'] and abs(balanced['duration']-2)<0.2
    call('run',['--fresh'])
    assert not any(r.get('resumed') for r in json.loads((batch/'report.json').read_text(encoding='utf-8'))['results'])
    call('publish',['--out',str(final)])
    assert len(list(final.iterdir()))==3 and all(x.suffix=='.mp3' for x in final.iterdir())
    (final/'old.txt').write_text('preserve me',encoding='utf-8')
    call('publish',['--out',str(final)],expected=2)
    assert (final/'old.txt').read_text(encoding='utf-8')=='preserve me'
    call('publish',['--out',str(final),'--replace-confirmed'])
    assert len(list(final.iterdir()))==3
    assert any((b/'old.txt').exists() for b in batch.glob('previous-output-*'))
    data['tracks'][0]['start']=1; write()
    call('publish',['--out',str(root/'must-not-publish')],expected=2)
    data['tracks'][0]['start']=0
    data['tracks'][1]['confirmed']=False; write()
    call('run',expected=2)
    call('publish',['--out',str(root/'pending-must-not-publish')],expected=2)
    data['tracks'][0]['source_file']='missing.wav'
    data['tracks'][2]['start']=2
    write()
    call('run',expected=2)
    mixed = json.loads((batch/'report.json').read_text(encoding='utf-8'))['results']
    assert [r['status'] for r in mixed]==['failed','pending','verified'], 'Failure blocked later tracks'
    print(json.dumps({'passed':True,'checks':['320kbps','full decode','trim duration','actual 0.3s fade envelope','author filename spaces','MP3-only publication','external backup','changed-manifest rejection','pending-version block'],'work':str(root)},ensure_ascii=False))

if __name__=='__main__':
    main()
