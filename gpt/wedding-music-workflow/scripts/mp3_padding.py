"""Conservative recovery of missing, unused bytes at the end of one MP3 frame.

This is not a general MP3 repairer. A caller must still fully decode the result
with strict error handling and compare its duration with the selected version.
"""
import hashlib
from pathlib import Path


def _reject(message):
    raise ValueError('MP3 tail recovery refused: ' + message)


def _header(data, offset):
    if offset + 4 > len(data):
        _reject('incomplete frame header')
    header = int.from_bytes(data[offset:offset+4], 'big')
    sample_index = (header >> 10) & 3
    if (header >> 21 != 0x7ff or (header >> 19) & 3 != 3 or (header >> 17) & 3 != 1
            or (header >> 12) & 15 != 14 or sample_index not in (0, 1)
            or (header >> 6) & 3 == 3 or header & 3 == 2):
        _reject('discontinuous or unsupported MPEG-1 Layer III 320 kbps stereo frames')
    if not (header >> 16) & 1:
        _reject('CRC-protected frame')
    sample_rate = (44100, 48000)[sample_index]
    return sample_rate, 144 * 320000 // sample_rate + ((header >> 9) & 1)


def _side_info(data, offset):
    """Read the fixed 32-byte MPEG-1 stereo side information, without decoding."""
    side = data[offset+4:offset+36]
    if len(side) != 32:
        _reject('incomplete side information')
    value, cursor = int.from_bytes(side, 'big'), 0

    def read(bits):
        nonlocal cursor
        cursor += bits
        if cursor > 256:
            _reject('side information overrun')
        return (value >> (256-cursor)) & ((1 << bits)-1)

    reservoir = read(9)
    read(3 + 8)  # Private bits and the two channels' scale-factor selection.
    main_bits = 0
    for _ in range(4):  # Two granules, two channels.
        main_bits += read(12)
        if read(9) > 288:
            _reject('invalid big-values count')
        read(8 + 4)
        if read(1):
            if read(2) == 0:
                _reject('invalid switched block type')
            read(1 + 10 + 9)
        else:
            read(15 + 4 + 3)
        read(3)
    if cursor != 256:
        _reject('inconsistent side information length')
    return reservoir, main_bits


def _inspect(data):
    if len(data) < 128 or data[-128:-125] != b'TAG':
        _reject('no terminal 128-byte ID3v1 tag')
    audio_start = 0
    if data[:3] == b'ID3':
        # Keep this recovery deliberately narrow: no extended header or footer.
        if len(data) < 10 or data[3] not in (3, 4) or data[4:6] != b'\0\0' or any(b & 128 for b in data[6:10]):
            _reject('unsupported leading ID3 header')
        audio_start = 10 + sum(b << shift for b, shift in zip(data[6:10], (21, 14, 7, 0)))
    tag_start, offset, count = len(data)-128, audio_start, 0
    sample_rate, declared_frames, declared_bytes = None, None, None
    while offset < tag_start:
        current_rate, frame_size = _header(data, offset)
        if sample_rate is not None and current_rate != sample_rate:
            _reject('sample rate changes within stream')
        sample_rate = current_rate
        if offset + 36 > tag_start:
            _reject('audio header or side information overlaps ID3v1')
        reservoir, main_bits = _side_info(data, offset)
        if main_bits > ((frame_size-36) + reservoir) * 8:
            _reject('audio payload exceeds the declared frame and reservoir')
        if count == 0:
            info = offset + 36
            if data[info:info+4] != b'Info':
                _reject('no CBR Info header at its standard position')
            flags = int.from_bytes(data[info+4:info+8], 'big')
            if flags & 3 != 3 or flags & ~15:
                _reject('Info header lacks trustworthy frame and byte counts')
            declared_frames = int.from_bytes(data[info+8:info+12], 'big')
            declared_bytes = int.from_bytes(data[info+12:info+16], 'big')
        count += 1
        frame_end = offset + frame_size
        if frame_end >= tag_start:
            last_offset, last_end = offset, frame_end
            last_reservoir, last_main_bits = reservoir, main_bits
            break
        offset = frame_end
    if count < 3:
        _reject('insufficient complete frame evidence')
    padding = last_end-tag_start
    if padding <= 0 or padding >= last_end-last_offset-36:
        _reject('no bounded, partial final-frame padding gap')
    if declared_frames != count-1 or declared_bytes != last_end-audio_start:
        _reject('Info counts do not exactly confirm the missing tail bytes')
    if last_reservoir != 0:
        _reject('last frame uses the bit reservoir')
    main_end = last_offset + 36 + (last_main_bits+7)//8
    if main_end > tag_start:
        _reject('missing bytes include required audio data')
    return {'recovery':'zero-fill-unused-final-frame-ancillary', 'padding_added':padding,
            'frame_count':count, 'audio_frame_count':count-1,
            'audio_start':audio_start, 'tail_offset':tag_start, 'last_frame_offset':last_offset,
            'last_frame_end_before_tag_move':last_end, 'main_data_end':main_end,
            'main_data_begin':last_reservoir, 'last_main_data_bits':last_main_bits,
            'xing_frames':declared_frames, 'xing_bytes':declared_bytes,
            'bitrate':320000, 'sample_rate':sample_rate, 'channels':2}


def recover_mp3_tail(source, dest):
    """Create a new file with proven missing ancillary zeros inserted before TAG.

    Every original byte is preserved in order; no audio frame is discarded or
    re-encoded. Refuse uncertain inputs and existing destinations. Success only
    proves the narrow structural recovery, not complete audio validity.
    """
    source, dest = Path(source).resolve(), Path(dest).resolve()
    if source == dest:
        _reject('source and destination are the same')
    original = source.read_bytes()
    evidence = _inspect(original)
    at = evidence['tail_offset']
    recovered = original[:at] + bytes(evidence['padding_added']) + original[at:]
    # Do not create or truncate a destination until every proof has passed.
    with dest.open('xb') as stream:
        try:
            stream.write(recovered)
        except Exception:
            stream.close()
            dest.unlink(missing_ok=True)
            raise
    evidence.update(source_bytes=len(original), output_bytes=len(recovered),
                    source_sha256=hashlib.sha256(original).hexdigest(),
                    output_sha256=hashlib.sha256(recovered).hexdigest())
    return evidence
