from __future__ import annotations

import concurrent.futures
import logging
import math
import random
import time
import typing
import urllib.parse

from librespot.audio import GeneralAudioStream
from librespot.audio import GeneralWritableStream
from librespot.audio import StreamId
from librespot.audio.AbsChunkedInputStream import AbsChunkedInputStream
from librespot.audio.decrypt import AesAudioDecrypt
from librespot.audio.decrypt import NoopAudioDecrypt
from librespot.audio.format import SuperAudioFormat
from librespot.audio.storage import ChannelManager
from librespot.common import Utils
from librespot.proto import StorageResolve_pb2 as StorageResolve

if typing.TYPE_CHECKING:
    from librespot.audio.decrypt.AudioDecrypt import AudioDecrypt
    from librespot.audio.HaltListener import HaltListener
    from librespot.cache.CacheManager import CacheManager
    from librespot.core.Session import Session
    from librespot.proto import Metadata_pb2 as Metadata


class CdnManager:
    _LOGGER: logging = logging.getLogger(__name__)
    _session: Session

    def __init__(self, session: Session):
        self._session = session

    def get_head(self, file_id: bytes):
        resp = self._session.client().get(
            self._session.get_user_attribute(
                "head-files-url",
                "https://heads-fa.spotify.com/head/{file_id}").replace(
                    "{file_id}", Utils.bytes_to_hex(file_id)))

        if resp.status_code != 200:
            raise IOError("{}".format(resp.status_code))

        body = resp.content
        if body is None:
            raise IOError("Response body is empty!")

        return body

    def stream_external_episode(self, episode: Metadata.Episode,
                                external_url: str,
                                halt_listener: HaltListener):
        return CdnManager.Streamer(
            self._session,
            StreamId.StreamId(episode),
            SuperAudioFormat.MP3,
            CdnManager.CdnUrl(self, None, external_url),
            self._session.cache(),
            NoopAudioDecrypt(),
            halt_listener,
        )

    def stream_file(
        self,
        file: Metadata.AudioFile,
        key: bytes,
        url: str,
        halt_listener: HaltListener,
    ):
        return CdnManager.Streamer(
            self._session,
            StreamId.StreamId(file),
            SuperAudioFormat.get(file.format),
            CdnManager.CdnUrl(self, file.file_id, url),
            self._session.cache(),
            AesAudioDecrypt(key),
            halt_listener,
        )

    def get_audio_url(self, file_id: bytes):
        resp = self._session.api().send(
            "GET",
            "/storage-resolve/files/audio/interactive/{}".format(
                Utils.bytes_to_hex(file_id)),
            None,
            None,
        )

        if resp.status_code != 200:
            raise IOError(resp.status_code)

        body = resp.content
        if body is None:
            raise IOError("Response body is empty!")

        proto = StorageResolve.StorageResolveResponse()
        proto.ParseFromString(body)
        if proto.result == StorageResolve.StorageResolveResponse.Result.CDN:
            url = random.choice(proto.cdnurl)
            self._LOGGER.debug("Fetched CDN url for {}: {}".format(
                Utils.bytes_to_hex(file_id), url))
            return url
        raise CdnManager.CdnException(
            "Could not retrieve CDN url! result: {}".format(proto.result))

    class CdnException(Exception):
        pass

    class InternalResponse:
        _buffer: bytearray
        _headers: typing.Dict[str, str]

        def __init__(self, buffer: bytearray, headers: typing.Dict[str, str]):
            self._buffer = buffer
            self._headers = headers

    class CdnUrl:
        __cdnManager = None
        __fileId: bytes
        _expiration: int
        _url: str

        def __init__(self, cdn_manager, file_id: bytes, url: str):
            self.__cdnManager: CdnManager = cdn_manager
            self.__fileId = file_id
            self.set_url(url)

        def url(self):
            if self._expiration == -1:
                return self._url

            if self._expiration <= int(time.time() * 1000) + 5 * 60 * 1000:
                self._url = self.__cdnManager.get_audio_url(self.__fileId)

            return self.url

        def set_url(self, url: str):
            self._url = url

            if self.__fileId is not None:
                token_url = urllib.parse.urlparse(url)
                token_query = urllib.parse.parse_qs(token_url.query)
                token_list = token_query.get("__token__")
                try:
                    token_str = str(token_list[0])
                except TypeError:
                    token_str = ""
                if token_str != "None" and len(token_str) != 0:
                    expire_at = None
                    split = token_str.split("~")
                    for s in split:
                        try:
                            i = s.index("=")
                        except ValueError:
                            continue

                        if s[:i] == "exp":
                            expire_at = int(s[i + 1:])
                            break

                    if expire_at is None:
                        self._expiration = -1
                        self.__cdnManager._LOGGER.warning(
                            "Invalid __token__ in CDN url: {}".format(url))
                        return

                    self._expiration = expire_at * 1000
                else:
                    try:
                        i = token_url.query.index("_")
                    except ValueError:
                        self._expiration = -1
                        self.__cdnManager._LOGGER.warning(
                            "Couldn't extract expiration, invalid parameter in CDN url: {}"
                            .format(url))
                        return

                    self._expiration = int(token_url.query[:i]) * 1000

            else:
                self._expiration = -1

    class Streamer(
            GeneralAudioStream.GeneralAudioStream,
            GeneralWritableStream.GeneralWritableStream,
    ):
        _session: Session
        _streamId: StreamId.StreamId
        _executorService = concurrent.futures.ThreadPoolExecutor()
        _audioFormat: SuperAudioFormat
        _audioDecrypt: AudioDecrypt
        _cdnUrl = None
        _size: int
        _buffer: typing.List[bytearray]
        _available: typing.List[bool]
        _requested: typing.List[bool]
        _chunks: int
        _internalStream: CdnManager.Streamer.InternalStream
        _haltListener: HaltListener

        def __init__(
            self,
            session: Session,
            stream_id: StreamId.StreamId,
            audio_format: SuperAudioFormat,
            cdn_url,
            cache: CacheManager,
            audio_decrypt: AudioDecrypt,
            halt_listener: HaltListener,
        ):
            self._session = session
            self._streamId = stream_id
            self._audioFormat = audio_format
            self._audioDecrypt = audio_decrypt
            self._cdnUrl = cdn_url
            self._haltListener = halt_listener

            resp = self.request(range_start=0,
                                range_end=ChannelManager.CHUNK_SIZE - 1)
            content_range = resp._headers.get("Content-Range")
            if content_range is None:
                raise IOError("Missing Content-Range header!")

            split = Utils.split(content_range, "/")
            self._size = int(split[1])
            self._chunks = int(
                math.ceil(self._size / ChannelManager.CHUNK_SIZE))

            first_chunk = resp._buffer

            self._available = [False for _ in range(self._chunks)]
            self._requested = [False for _ in range(self._chunks)]
            self._buffer = [bytearray() for _ in range(self._chunks)]
            self._internalStream = CdnManager.Streamer.InternalStream(
                self, False)

            self._requested[0] = True
            self.write_chunk(first_chunk, 0, False)

        def write_chunk(self, chunk: bytes, chunk_index: int,
                        cached: bool) -> None:
            if self._internalStream.is_closed():
                return

            self._session._LOGGER.debug(
                "Chunk {}/{} completed, cached: {}, stream: {}".format(
                    chunk_index + 1, self._chunks, cached, self.describe()))

            self._buffer[chunk_index] = self._audioDecrypt.decrypt_chunk(
                chunk_index, chunk)
            self._internalStream.notify_chunk_available(chunk_index)

        def stream(self) -> AbsChunkedInputStream:
            return self._internalStream

        def codec(self) -> SuperAudioFormat:
            return self._audioFormat

        def describe(self) -> str:
            if self._streamId.is_episode():
                return "episode_gid: {}".format(
                    self._streamId.get_episode_gid())
            return "file_id: {}".format(self._streamId.get_file_id())

        def decrypt_time_ms(self) -> int:
            return self._audioDecrypt.decrypt_time_ms()

        def request_chunk(self, index: int) -> None:
            resp = self.request(index)
            self.write_chunk(resp._buffer, index, False)

        def request(self,
                    chunk: int = None,
                    range_start: int = None,
                    range_end: int = None) -> CdnManager.InternalResponse:
            if chunk is None and range_start is None and range_end is None:
                raise TypeError()

            if chunk is not None:
                range_start = ChannelManager.CHUNK_SIZE * chunk
                range_end = (chunk + 1) * ChannelManager.CHUNK_SIZE - 1

            resp = self._session.client().get(
                self._cdnUrl._url,
                headers={
                    "Range": "bytes={}-{}".format(range_start, range_end)
                },
            )

            if resp.status_code != 206:
                raise IOError(resp.status_code)

            body = resp.content
            if body is None:
                raise IOError("Response body is empty!")

            return CdnManager.InternalResponse(bytearray(body), resp.headers)

        class InternalStream(AbsChunkedInputStream):
            streamer = None

            def __init__(self, streamer, retry_on_chunk_error: bool):
                self.streamer: CdnManager.Streamer = streamer
                super().__init__(retry_on_chunk_error)

            def buffer(self) -> typing.List[bytearray]:
                return self.streamer._buffer

            def size(self) -> int:
                return self.streamer._size

            def requested_chunks(self) -> typing.List[bool]:
                return self.streamer._requested

            def available_chunks(self) -> typing.List[bool]:
                return self.streamer._available

            def chunks(self) -> int:
                return self.streamer._chunks

            def request_chunk_from_stream(self, index: int) -> None:
                self.streamer._executorService.submit(
                    lambda: self.streamer.request_chunk(index))

            def stream_read_halted(self, chunk: int, _time: int) -> None:
                if self.streamer._haltListener is not None:
                    self.streamer._executorService.submit(
                        lambda: self.streamer._haltListener.stream_read_halted(
                            chunk, _time))

            def stream_read_resumed(self, chunk: int, _time: int) -> None:
                if self.streamer._haltListener is not None:
                    self.streamer._executorService.submit(
                        lambda: self.streamer._haltListener.
                        stream_read_resumed(chunk, _time))
