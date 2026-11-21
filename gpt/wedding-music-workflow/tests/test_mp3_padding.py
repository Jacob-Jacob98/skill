"""Self-contained structural boundary tests for the narrow MP3 tail recovery."""
from pathlib import Path
import tempfile
import unittest

from mp3_padding import recover_mp3_tail


def side_info(lengths=(32, 32, 0, 0), reservoir=0):
    bits = f'{reservoir:09b}' + '0' * 11
    for length in lengths:
        bits += f'{length:012b}' + '0' * 9 + f'{210:08b}' + '0' * 4 + '0'
        bits += '0' * (15+4+3+3)
    assert len(bits) == 256
    return int(bits, 2).to_bytes(32, 'big')


def sample(missing=7, lengths=(32,32,0,0), reservoir=0):
    header = bytes.fromhex('fffbe064')
    frame_size = 1044
    info = bytearray(header + bytes(frame_size-4))
    info[36:52] = b'Info' + (3).to_bytes(4,'big') + (2).to_bytes(4,'big') + (frame_size*3).to_bytes(4,'big')
    normal = header + side_info() + bytes(frame_size-36)
    last = header + side_info(lengths, reservoir) + bytes(frame_size-36)
    # A small, ordinary ID3v2.3 header and a complete terminal ID3v1 tag.
    return b'ID3\x03\x00\x00\x00\x00\x00\x00' + bytes(info) + normal + last[:-missing] + b'TAG' + bytes(125)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(root))
        self.source, self.dest = self.root/'source.mp3', self.root/'recovered.mp3'

    def tearDown(self):
        self.temp.cleanup()

    def reject(self, data):
        self.source.write_bytes(data)
        with self.assertRaises(ValueError):
            recover_mp3_tail(self.source, self.dest)
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.source.read_bytes(), data)

    def test_inserts_only_proven_ancillary_zeros(self):
        original = sample()
        self.source.write_bytes(original)
        evidence = recover_mp3_tail(self.source, self.dest)
        output = self.dest.read_bytes()
        self.assertEqual(evidence['padding_added'], 7)
        self.assertEqual(evidence['frame_count'], 3)
        self.assertEqual(evidence['audio_frame_count'], 2)
        self.assertEqual(output, original[:-128] + bytes(7) + original[-128:])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(evidence['source_bytes']+7, evidence['output_bytes'])

    def test_single_missing_byte_is_recoverable(self):
        self.source.write_bytes(sample(1))
        self.assertEqual(recover_mp3_tail(self.source, self.dest)['padding_added'], 1)

    def test_never_overwrites_destination(self):
        self.source.write_bytes(sample())
        self.dest.write_bytes(b'preserve existing')
        with self.assertRaises(FileExistsError):
            recover_mp3_tail(self.source,self.dest)
        self.assertEqual(self.dest.read_bytes(),b'preserve existing')

    def test_same_path_rejected(self):
        self.source.write_bytes(sample())
        with self.assertRaises(ValueError):
            recover_mp3_tail(self.source,self.source)

    def test_crc_protection_rejected_in_any_frame(self):
        data = bytearray(sample())
        data[10+1044+1] &= 254
        self.reject(bytes(data))

    def test_internal_frame_discontinuity_rejected(self):
        data = sample()
        self.reject(data[:1100] + data[1101:])

    def test_sample_rate_change_rejected(self):
        data = bytearray(sample())
        data[10+1044+2] |= 4
        self.reject(bytes(data))

    def test_missing_audio_bits_rejected(self):
        self.reject(sample(120, lengths=(4000,4000,0,0)))

    def test_final_bit_reservoir_rejected(self):
        self.reject(sample(reservoir=1))

    def test_info_byte_count_mismatch_rejected(self):
        data = bytearray(sample())
        data[10+51] ^= 1
        self.reject(bytes(data))

    def test_info_frame_count_mismatch_rejected(self):
        data = bytearray(sample())
        data[10+47] ^= 1
        self.reject(bytes(data))

    def test_no_info_header_rejected(self):
        data = bytearray(sample())
        data[46:50] = b'Xing'
        self.reject(bytes(data))

    def test_no_terminal_tag_rejected(self):
        self.reject(sample()[:-128])

    def test_already_complete_stream_rejected(self):
        original = sample()
        self.reject(original[:-128] + bytes(7) + original[-128:])

    def test_unsupported_leading_tag_rejected(self):
        data = bytearray(sample())
        data[5] = 64
        self.reject(bytes(data))

    def test_invalid_side_info_rejected(self):
        # First channel's big_values has only 288 legal entries.
        data = bytearray(sample())
        offset = 10+1044+4
        value = int.from_bytes(data[offset:offset+32], 'big')
        value |= 511 << (256-20-12-9)
        data[offset:offset+32] = value.to_bytes(32,'big')
        self.reject(bytes(data))


if __name__ == '__main__':
    unittest.main()
