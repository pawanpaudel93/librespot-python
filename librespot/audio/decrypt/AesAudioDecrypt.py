import time

from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter

from librespot.audio.decrypt.AudioDecrypt import AudioDecrypt
from librespot.audio.storage import ChannelManager


class AesAudioDecrypt(AudioDecrypt):
    audio_aes_iv = bytes([
        0x72,
        0xE0,
        0x67,
        0xFB,
        0xDD,
        0xCB,
        0xCF,
        0x77,
        0xEB,
        0xE8,
        0xBC,
        0x64,
        0x3F,
        0x63,
        0x0D,
        0x93,
    ])
    iv_int = int.from_bytes(audio_aes_iv, "big")
    iv_diff = 0x100
    cipher = None
    decrypt_count = 0
    decrypt_total_time = 0
    key: bytes

    def __init__(self, key: bytes):
        self.key = key

    def decrypt_chunk(self, chunk_index: int, buffer: bytes):
        new_buffer = b""
        iv = self.iv_int + int(ChannelManager.CHUNK_SIZE * chunk_index / 16)
        start = time.time_ns()
        for i in range(0, len(buffer), 4096):
            cipher = AES.new(
                key=self.key,
                mode=AES.MODE_CTR,
                counter=Counter.new(128, initial_value=iv),
            )

            count = min(4096, len(buffer) - i)
            decrypted_buffer = cipher.decrypt(buffer[i:i + count])
            new_buffer += decrypted_buffer
            if count != len(decrypted_buffer):
                raise RuntimeError(
                    "Couldn't process all data, actual: {}, expected: {}".
                    format(len(decrypted_buffer), count))

            iv += self.iv_diff

        self.decrypt_total_time += time.time_ns() - start
        self.decrypt_count += 1

        return new_buffer

    def decrypt_time_ms(self):
        return (0 if self.decrypt_count == 0 else int(
            (self.decrypt_total_time / self.decrypt_count) / 1000000))
