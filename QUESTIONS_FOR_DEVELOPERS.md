# Questions for Zello Channel API Developers

## Problem Context
We are integrating Zello Channel API into a Python application for sending voice responses. The problem: Zello server stops the outgoing audio stream immediately after receiving the first packet (`on_stream_stop`), and users do not receive voice responses.

## Technical Details

### Incoming Stream (from Zello users)
- **TOC byte:** `0x18` (binary: `00011000`)
- **Config ID:** 0 or 3 (Narrowband 8kHz, 60ms)
- **Stereo:** 1 (stereo)
- **Frame Count:** 1
- **codec_header:** `gD4BPA==` (decodes to 16000 Hz, 1 frame, 60ms)

**Question 1:** Why does `codec_header` show 16000 Hz, but TOC byte indicates Config 0/3 (8kHz Narrowband)? Is this a mismatch or are we incorrectly interpreting the data?

### Outgoing Stream (from our bot)
- **TOC byte:** `0x5c` (binary: `01011100`) or `0x1c` (binary: `00011100`)
- **Config ID:** 11 (Wideband 16kHz, 60ms) or 3 (Narrowband 8kHz, 60ms)
- **Stereo:** 1 (stereo)
- **Frame Count:** 0 or 1
- **codec_header:** `gD4BPA==` (16000 Hz, 1 frame, 60ms) or `QB8BPA==` (8000 Hz, 1 frame, 60ms)

**Question 2:** What exact Config ID should be used for outgoing streams? Should it match the incoming stream (Config 0/3 for 8kHz) or can we use Config 11 (16kHz)?

**Question 3:** Are we parsing the TOC byte correctly? According to RFC 6716:
- Bits 0-4: Config (5 bits)
- Bit 5: Stereo flag
- Bits 6-7: Frame count code

But for `0x18` this gives Config=24, which is clearly incorrect. Does Zello use a different TOC byte format?

### Binary Packet Structure
We send packets in the following format:
```
Byte 0: 0x01 (Packet Type)
Bytes 1-4: Stream ID (uint32, Big Endian)
Bytes 5-8: Packet ID (uint32, Big Endian)
Bytes 9+: Opus payload (raw Opus packet)
```

**Question 4:** Is this structure correct? Are there any additional requirements for packet format?

**Question 5:** Are we using Big Endian correctly for Stream ID and Packet ID? Some examples use Little Endian.

### Opus Encoder Parameters
Current parameters:
- Sample Rate: 16000 Hz (or 8000 Hz when trying to match incoming stream)
- Channels: 2 (stereo)
- Application: APPLICATION_VOIP
- Bitrate: 24000 bps
- Frame Duration: 60ms
- Frame Size: 960 samples (for 16kHz) or 480 samples (for 8kHz)

**Question 6:** What exact Opus encoder parameters should be used for outgoing streams? Are there any specific Zello requirements?

**Question 7:** Should the `codec_header` in the `start_stream` command exactly match the Opus packet encoding parameters? Or does the server automatically convert?

### Stereo Data Interleaving
We convert mono PCM to stereo with interleaving (LRLR format):
```
Left[0], Right[0], Left[1], Right[1], ...
```

**Question 8:** Is this format correct? Does Zello require interleaved format for stereo?

### Stream Stop Problem
The server sends `on_stream_stop` immediately after receiving the first packet. This happens before we can send all packets.

**Question 9:** Why does the server stop the stream after the first packet? Is this an error in packet format, incorrect Config ID, or something else?

**Question 10:** Is there a minimum number of packets that must be sent? Or a minimum stream duration?

**Question 11:** Can the server stop the stream due to a mismatch between `codec_header` and actual Opus packet parameters?

### Frame Count Code in TOC Byte
Incoming stream has Frame Count Code = 1, outgoing = 0.

**Question 12:** What does Frame Count Code mean in Zello context? Should it be the same for incoming and outgoing streams?

### Reference Implementation
**Question 13:** Is there a reference Python implementation for sending audio via Zello Channel API? We are particularly interested in correct Opus encoding and binary packet formation.

**Question 14:** Can you provide a working code example that successfully sends voice messages to a channel?

### Additional Questions
**Question 15:** Is there server-side logging that can help diagnose the problem? What errors are visible on the server side when receiving our packets?

**Question 16:** Are any special permissions or channel settings required for sending audio? Could this be a permissions issue?

**Question 17:** Are there any limitations on audio duration or number of packets in a single stream?

**Question 18:** Are we handling the `start_stream` response correctly? We wait for `stream_id` and only then send packets. Are there any timeouts or other requirements?

## Current Implementation
- Using `opuslib` library for Opus encoding
- Sending binary packets via WebSocket
- Using correct 9-byte header format
- Sending packets with 60ms delay between them
- Sending `stop_stream` command after all packets

## What We've Tried
1. Encoding at 16kHz (Config 11) - server stops stream
2. Encoding at 8kHz (Config 0/3) - server stops stream
3. Different encoder parameters (APPLICATION_VOIP, APPLICATION_AUDIO)
4. Different bitrates (16kbps, 24kbps)
5. Correct stereo data interleaving
6. Correct frame_size (samples per channel)

None of these approaches worked - the server always stops the stream after the first packet.

## Contact Information
We are ready to provide:
- Full WebSocket connection logs
- Hex dumps of sent packets
- Code examples
- Detailed configuration information

Thank you for your help!
