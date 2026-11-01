import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from audio_formats import prepare_compatible_audio, probe_audio_stream, AudioFormatError
from test_downloads import ROOT

FFMPEG = ROOT/'ffmpeg-master-latest-win64-gpl-shared/bin/ffmpeg.exe'
FFPROBE = FFMPEG.with_name('ffprobe.exe')


class AudioCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make(self, filename, codec, extra=()):
        path = self.root/filename
        subprocess.run([str(FFMPEG),'-nostdin','-v','error','-y','-f','lavfi','-i',
                        'sine=frequency=440:sample_rate=48000:duration=0.5','-ac','2',
                        '-c:a',codec,*extra,'-metadata','title=Песня','-metadata','artist=Артист',
                        str(path)],capture_output=True,check=True,timeout=30)
        return path

    def test_opus_and_vorbis_become_aac_m4a_with_unicode_tags(self):
        for filename,codec in [('Опус.opus','libopus'),('Ворбис.ogg','libvorbis')]:
            with self.subTest(codec=codec):
                source = self.make(filename,codec)
                target = Path(prepare_compatible_audio(source,FFMPEG,self.root))
                self.assertEqual(target.suffix,'.m4a')
                info = probe_audio_stream(target,FFPROBE)
                self.assertEqual(info['codec_name'],'aac')
                self.assertEqual(info['channels'],2)
                self.assertEqual(info['sample_rate'],'48000')
                self.assertFalse(source.exists())
                result = subprocess.run([str(FFPROBE),'-v','error','-show_entries','format_tags=title,artist',
                                         '-of','json',str(target)],capture_output=True,check=True)
                tags = json.loads(result.stdout)['format']['tags']
                self.assertEqual(tags['title'],'Песня')
                self.assertEqual(tags['artist'],'Артист')

    def test_raw_aac_is_remuxed_without_reencoding_audio(self):
        source = self.make('AAC.aac','aac',('-b:a','256k'))
        before = source.read_bytes()
        target = Path(prepare_compatible_audio(source,FFMPEG,self.root))
        self.assertEqual(probe_audio_stream(target,FFPROBE)['codec_name'],'aac')
        restored = self.root/'restored.aac'
        subprocess.run([str(FFMPEG),'-v','error','-i',str(target),'-map','0:a:0','-c:a','copy',
                        '-f','adts',str(restored)],capture_output=True,check=True,timeout=30)
        self.assertEqual(restored.read_bytes(),before)

    def test_flac_inside_ogg_remains_lossless_flac(self):
        from mutagen.flac import FLAC
        reference = self.make('reference.flac','flac')
        source = self.root/'lossless.ogg'
        subprocess.run([str(FFMPEG),'-v','error','-i',str(reference),'-c:a','copy','-f','ogg',str(source)],
                       capture_output=True,check=True,timeout=30)
        target = Path(prepare_compatible_audio(source,FFMPEG,self.root))
        self.assertEqual(target.suffix,'.flac')
        self.assertEqual(FLAC(target).info.md5_signature,FLAC(reference).info.md5_signature)

    def test_native_mp3_m4a_and_flac_are_unchanged(self):
        for extension in ('.mp3','.m4a','.flac'):
            source = self.root/('audio'+extension)
            source.write_bytes(b'unchanged')
            with patch('audio_formats.subprocess.run') as run:
                target = prepare_compatible_audio(source,FFMPEG,self.root)
            run.assert_not_called()
            self.assertEqual(Path(target),source)
            self.assertEqual(source.read_bytes(),b'unchanged')

    def test_conversion_error_keeps_source_and_removes_partial_output(self):
        source = self.make('error.opus','libopus')
        actual_run = subprocess.run
        def run(command,**kwargs):
            if str(command[0]) == str(FFMPEG):
                Path(command[-1]).write_bytes(b'partial')
                return subprocess.CompletedProcess(command,1,b'',b'error')
            return actual_run(command,**kwargs)
        with patch('audio_formats.subprocess.run',side_effect=run):
            with self.assertRaises(AudioFormatError):
                prepare_compatible_audio(source,FFMPEG,self.root)
        self.assertTrue(source.is_file())
        self.assertFalse(source.with_suffix('.m4a').exists())


if __name__ == '__main__':
    unittest.main()
