"""Адаптер MAX Messenger поверх pyromax."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from http import HTTPStatus
from pathlib import Path
from typing import Any

import aiohttp
from pyromax import Dispatcher as MaxDispatcher
from pyromax import MaxApi
from pyromax.mapping.envelope.constants import Opcode
from pyromax.mapping.envelope.v11.methods.immutable.files import GetFileLinkMethod
from pyromax.mapping.envelope.v11.payloads.models import (
    ControlMappingModel,
    FileMappingModel,
    PhotoMappingModel,
    ShareMappingModel,
    VideoMappingModel,
    VideoNoteMappingModel,
    VoiceMappingModel,
)
from pyromax.mapping.envelope.v11.translate.ToDTO.FileTranslate import (
    MAPPING_MODEL_TO_FILE_MAPPING,
    VideoNoteMapping,
    VoiceMapping,
)
from pyromax.methods import SendMessageMethod
from pyromax.models import Message, MessageLink
from pyromax.models.Attachments import (
    FileAttachment,
    PhotoAttachment,
    VideoAttachment,
)

from ..config import Settings
from ..models import (
    AttachmentKind,
    NormalizedAttachment,
    NormalizedMessage,
    Platform,
    RemoteChat,
    TextSpan,
)
from ..ratelimit import RateLimiter
from ..storage import Storage
from ..util.media import human_size, safe_filename, to_telegram_photo, webp_to_png
from ..util.net import read_limited
from ..util.text import (
    max_elements_to_spans,
    spans_to_max_elements,
    spans_to_max_tags,
)

logger = logging.getLogger("max")

#: Голосовые и «кружки» не зарегистрированы в таблице скачивания pyromax 0.8,
#: хотя классы для них есть. Дополняем таблицу, иначе download_file падает.
MAPPING_MODEL_TO_FILE_MAPPING.setdefault(VoiceMappingModel, VoiceMapping)
MAPPING_MODEL_TO_FILE_MAPPING.setdefault(VideoNoteMappingModel, VideoNoteMapping)

#: Сколько ждать перед разбором собственного сообщения: за это время в базе
#: успевает появиться запись о сообщении, отправленном самим мостом.
ECHO_GRACE_SECONDS = 2.0

#: Идентификатор «Избранного» — личного чата с самим собой.
FAVORITES_CHAT_ID = 0

#: Ограничения на картинку аватара.
AVATAR_TIMEOUT = 30
AVATAR_SIZE_LIMIT = 10 * 1024 * 1024

#: Хранилище картинок MAX стоит за той же инфраструктурой, что и вложения,
#: и без привычных ему заголовков отдаёт вместо файла заглушку.
AVATAR_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
    "Referer": "https://web.max.ru/",
}
AVATAR_COOKIES = {"tstc": "p"}

#: Заголовки для скачивания вложений: медиа лежит на инфраструктуре ОК.
DOWNLOAD_HEADERS = {
    "User-Agent": AVATAR_HEADERS["User-Agent"],
    "Accept": "*/*",
    "Referer": "https://ok.ru/",
}
DOWNLOAD_TIMEOUT = 120

#: Опкод выборки медиа по чату: в pyromax его нет, но именно он отдаёт
#: вложения вместе с готовыми ссылками на файлы.
GET_MEDIA_OPCODE = 51
#: Для каких вложений ссылку ищем в выборке медиа по чату. Видеосообщения
#: сюда не входят: там она отдаёт перекодированный файл, и Telegram перестаёт
#: принимать его как «кружок».
MEDIA_ATTACH_TYPES: dict[type, str] = {
    VoiceMappingModel: "AUDIO",
    PhotoMappingModel: "PHOTO",
}
#: Сколько соседних медиа запрашивать вокруг нужного сообщения.
MEDIA_HISTORY_DEPTH = 5

#: Поля вложения-стикера: сначала анимация, затем статичное превью.
ANIMATED_STICKER_KEYS = ("lottieUrl", "animationUrl", "webmUrl", "videoUrl")
STATIC_STICKER_KEYS = ("url", "baseUrl")

#: Длина цитаты, которая показывается вместо недоступного оригинала.
QUOTE_LIMIT = 120

#: Сколько раз пробовать отправку в MAX: обрыв websocket лечится
#: переподключением, которое занимает несколько секунд.
SEND_ATTEMPTS = 5

#: Вид вложения Telegram -> класс вложения MAX для выгрузки.
#:
#: Кружок и голосовое MAX принимает только от своих клиентов: путь загрузки
#: ``VideoNoteAttachment``/``VoiceAttachment`` сервер отвергает с
#: ``video.not.supported`` при любом кодеке (проверено на opus, aac, mp3 и
#: h264 baseline). Поэтому кружок уходит обычным видео, а голосовое — файлом:
#: содержимое доезжает целиком, теряется только форма подачи.
UPLOAD_TYPES: dict[AttachmentKind, type] = {
    AttachmentKind.PHOTO: PhotoAttachment,
    AttachmentKind.VIDEO: VideoAttachment,
    AttachmentKind.ANIMATION: VideoAttachment,
    AttachmentKind.VIDEO_NOTE: VideoAttachment,
    AttachmentKind.VOICE: FileAttachment,
    AttachmentKind.AUDIO: FileAttachment,
    AttachmentKind.DOCUMENT: FileAttachment,
    AttachmentKind.STICKER: FileAttachment,
}

#: Пометки к вложениям, форму которых MAX не воспроизводит.
DEGRADED_NOTES: dict[AttachmentKind, str] = {
    AttachmentKind.VOICE: "🎤 голосовое сообщение",
    AttachmentKind.VIDEO_NOTE: "⭕ видеосообщение (кружок)",
}


class MaxAdapter:
    """Адаптер платформы MAX: приём событий и отправка сообщений."""

    platform = Platform.MAX

    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        on_message: Callable[[NormalizedMessage], Awaitable[None]],
        account_id: int = 0,
        token: str | None = None,
        nickname: str = "MAX",
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._on_message = on_message
        #: Аккаунт MAX, который обслуживает эта сессия.
        self.account_id = account_id
        self.nickname = nickname
        self._token = token
        self._limiter = RateLimiter(settings.max_global_rate, settings.max_chat_rate)
        self._api: MaxApi | None = None
        self._dispatcher = MaxDispatcher()
        self._names: dict[int, str] = {}
        self._chat_cache: dict[int, RemoteChat] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        #: Куда отправить ссылку авторизации, если аккаунт ещё не подключён.
        self._qr_callback: Callable[[str], Awaitable[None]] | None = None
        self._register_handlers()

    def set_qr_callback(self, callback: Callable[[str], Awaitable[None]] | None) -> None:
        """Задать способ показать пользователю ссылку авторизации MAX."""
        self._qr_callback = callback

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Подключиться к MAX (при первом запуске — авторизация по QR-коду)."""
        logger.info("Подключаемся к MAX (device_type=%s)...", self._settings.max_device_type)
        self._api = await MaxApi(
            device_type=self._settings.max_device_type,
            password=self._settings.max_password,
            token=self._token,
            token_suffix=self._token_suffix(),
            url_callback=self._qr_callback or self._show_login_qr,
        )
        logger.info(
            "MAX подключён: аккаунт %s, id=%s, телефон=%s",
            self.account_id,
            self._api.id,
            self._api.phone,
        )
        await self._remember_identity()
        await self._refresh_chats()

    def _token_suffix(self) -> str:
        """Суффикс имени токена: у каждого аккаунта своя запись в tokens.json."""
        base = self._settings.max_token_suffix or ""
        return f"{base}#{self.account_id}" if self.account_id else base

    async def _remember_identity(self) -> None:
        """Сохранить в базе токен и данные владельца аккаунта."""
        if not self.account_id or self._api is None:
            return
        token = getattr(self._api.mapper, "token", None)
        nickname = self.nickname
        profile = getattr(self._api, "me", None)
        contact = getattr(profile, "contact", None)
        names = getattr(contact, "names", None) or []
        if names:
            first = getattr(names[0], "name", None) or getattr(names[0], "first_name", None)
            if first:
                nickname = str(first)
        self.nickname = nickname
        await self._storage.save_account_token(
            self.account_id,
            token=token,
            max_user_id=self._api.id,
            nickname=nickname,
            phone=str(self._api.phone) if self._api.phone else None,
        )

    @staticmethod
    async def _show_login_qr(url: str) -> None:
        """Показать ссылку авторизации: ASCII-код в консоли и картинка на диске.

        Сканировать ASCII-код в перенаправленном выводе неудобно, поэтому тот же
        код сохраняется в PNG рядом с состоянием моста.
        """
        import qrcode

        code = qrcode.QRCode()
        code.add_data(url)
        code.make(fit=True)

        logger.warning("Требуется авторизация в MAX. Ссылка: %s", url)
        try:
            # Печать в консоль отделена от сохранения файла: на терминале без
            # поддержки блочных символов она падает, а картинка нужна всегда.
            code.print_ascii(invert=True)
        except (UnicodeEncodeError, OSError):
            logger.debug("Консоль не смогла отобразить QR-код", exc_info=True)
        try:
            path = await asyncio.to_thread(_save_qr_image, code)
        except Exception:
            logger.warning(
                "Не удалось сохранить QR-код картинкой — откройте ссылку выше вручную",
                exc_info=True,
            )
            return
        logger.warning("QR-код сохранён в %s — отсканируйте его в приложении MAX", path)

    async def run(self) -> None:
        """Слушать события MAX."""
        if self._api is None:
            raise RuntimeError("MaxAdapter.start() не вызван")
        await self._dispatcher.start_polling(max_api=self._api)

    async def stop(self) -> None:
        """Остановить фоновые задачи адаптера."""
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    @property
    def api(self) -> MaxApi:
        """Инициализированный клиент MAX."""
        if self._api is None:
            raise RuntimeError("MaxAdapter.start() не вызван")
        return self._api

    # ------------------------------------------------------------------ #
    # Приём событий MAX
    # ------------------------------------------------------------------ #

    def _register_handlers(self) -> None:
        dispatcher = self._dispatcher
        # from_me=True — свои сообщения тоже нужны: их пишут из клиента MAX.
        # Эхо собственных пересылок отсеивается по таблице соответствий.
        dispatcher.message(from_me=True)(self._handle_message)
        dispatcher.reply_to_message(from_me=True)(self._handle_message)
        dispatcher.forward_message(from_me=True)(self._handle_forward)
        dispatcher.edited_message(from_me=True)(self._handle_edited)

    async def _handle_message(self, message: Message) -> None:
        await self._dispatch(message, is_edit=False)

    async def _handle_forward(self, message: Message) -> None:
        await self._dispatch(message, is_edit=False, forwarded=True)

    async def _handle_edited(self, message: Message) -> None:
        await self._dispatch(message, is_edit=True)

    async def _dispatch(self, message: Message, *, is_edit: bool, forwarded: bool = False) -> None:
        """Решить, нужно ли пересылать событие, и передать его мосту."""
        binding = await self._storage.get_by_max(message.chat_id, self.account_id)
        if binding is None or not binding.enabled:
            return

        own = self._api is not None and message.sender_id == self._api.id
        if own:
            if not self._settings.max_forward_own_messages:
                return
            # Своё сообщение может оказаться эхом пересылки из Telegram: запись
            # о нём появляется чуть позже ответа сервера, поэтому ждём в фоне.
            task = asyncio.create_task(self._dispatch_own(message, is_edit, forwarded))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return

        await self._emit(message, is_edit=is_edit, forwarded=forwarded)

    async def _dispatch_own(self, message: Message, is_edit: bool, forwarded: bool) -> None:
        await asyncio.sleep(ECHO_GRACE_SECONDS)
        if await self._storage.is_mirrored_from_tg(
            message.chat_id, str(message.message_id), self.account_id
        ):
            return
        await self._emit(message, is_edit=is_edit, forwarded=forwarded)

    async def _emit(self, message: Message, *, is_edit: bool, forwarded: bool) -> None:
        normalized = await self._normalize(message, is_edit=is_edit, forwarded=forwarded)
        if normalized is None:
            return
        await self._on_message(normalized)

    async def _normalize(
        self, message: Message, *, is_edit: bool, forwarded: bool
    ) -> NormalizedMessage | None:
        """Привести сообщение MAX к нормализованному виду."""
        text = message.text or ""
        spans: list[TextSpan] = max_elements_to_spans(message.elements)

        reply_to: str | None = None
        reply_preview: str | None = None
        link = message.link
        if link is not None and link.type == "REPLY":
            # pyromax теряет message_id при разборе push-уведомления и оставляет
            # только вложенное сообщение — идентификатор берётся из него.
            target = link.message_id
            if target is None and link.message is not None:
                target = getattr(link.message, "message_id", None)
            if target is not None:
                reply_to = str(target)
            if link.message is not None:
                reply_preview = await self._quote(link.message)

        normalized = NormalizedMessage(
            source=Platform.MAX,
            source_chat_id=message.chat_id,
            source_message_id=str(message.message_id),
            account_id=self.account_id,
            author=await self._resolve_name(message.sender_id),
            text=text,
            spans=spans,
            reply_to_source_id=reply_to,
            reply_preview=reply_preview,
            is_edit=is_edit,
        )

        if forwarded and link is not None:
            origin = await self._describe_forward(link)
            if origin:
                normalized.notes.append(origin)

        for attach in message.attaches or []:
            _restore_context(attach, message)
            await self._convert_attachment(message, attach, normalized)

        return normalized if not normalized.is_empty else None

    async def _quote(self, original: Any) -> str:
        """Короткая цитата сообщения, на которое отвечают."""
        author = await self._resolve_name(getattr(original, "sender_id", None))
        text = " ".join((getattr(original, "text", "") or "").split())
        if not text:
            text = "вложение"
        if len(text) > QUOTE_LIMIT:
            text = text[: QUOTE_LIMIT - 1].rstrip() + "…"
        return f"{author}: {text}"

    async def _describe_forward(self, link: MessageLink) -> str:
        """Текстовая пометка о пересланном сообщении."""
        inner = link.message
        parts = ["↪️ Пересланное сообщение"]
        if inner is not None:
            author = await self._resolve_name(getattr(inner, "sender_id", None))
            parts[0] = f"↪️ Переслано от {author}"
            if inner.text:
                parts.append(inner.text)
        return "\n".join(parts)

    async def _convert_attachment(
        self, message: Message, attach: Any, normalized: NormalizedMessage
    ) -> None:
        """Превратить вложение MAX в нормализованное или в текстовую пометку."""
        if isinstance(attach, PhotoMappingModel):
            normalized.attachments.append(
                self._build(
                    attach,
                    AttachmentKind.PHOTO,
                    f"photo_{attach.photo_id or message.message_id}.jpg",
                    width=attach.width,
                    height=attach.height,
                )
            )
            return
        if isinstance(attach, VideoNoteMappingModel):
            normalized.attachments.append(
                self._build(
                    attach,
                    AttachmentKind.VIDEO_NOTE,
                    f"video_note_{attach.video_id}.mp4",
                    duration=attach.duration,
                )
            )
            return
        if isinstance(attach, VideoMappingModel):
            normalized.attachments.append(
                self._build(
                    attach,
                    AttachmentKind.VIDEO,
                    f"video_{attach.video_id}.mp4",
                    duration=attach.duration,
                    width=attach.width,
                    height=attach.height,
                )
            )
            return
        if isinstance(attach, VoiceMappingModel):
            normalized.attachments.append(
                self._build(
                    attach,
                    AttachmentKind.VOICE,
                    f"voice_{attach.audio_id}.ogg",
                    note="🎤 Голосовое сообщение — MAX не отдаёт его файлом, "
                    "послушайте в приложении",
                )
            )
            return
        if isinstance(attach, FileMappingModel):
            normalized.attachments.append(
                self._build(
                    attach,
                    AttachmentKind.DOCUMENT,
                    attach.name or f"file_{attach.file_id}.bin",
                    size=attach.size,
                )
            )
            return
        if isinstance(attach, ShareMappingModel):
            note = _describe_share(attach.url, attach.title, attach.description, normalized.text)
            if note:
                normalized.notes.append(note)
            return
        if isinstance(attach, ControlMappingModel):
            normalized.notes.append(f"ℹ️ Событие чата: {attach.event}")
            return

        # Стикер приходит словарём со ссылками: анимация и статичное превью.
        if isinstance(attach, dict) and str(attach.get("_type") or "").upper() == "STICKER":
            animated = _first_link(attach, ANIMATED_STICKER_KEYS)
            preview = _first_link(attach, STATIC_STICKER_KEYS)
            # Анимацию (Lottie) взять нельзя: Bot API разрешает боту отправлять
            # анимированные стикеры только по file_id уже существующего стикера,
            # а загруженный .tgs Telegram принимает, но не отрисовывает.
            source = preview or animated
            if source:
                logger.info(
                    "Стикер MAX: тип %s, статичное превью %s",
                    attach.get("stickerType"),
                    "есть" if preview else "нет",
                )
                normalized.attachments.append(
                    NormalizedAttachment(
                        kind=AttachmentKind.STICKER,
                        filename=f"sticker_{attach.get('stickerId') or 'max'}.png",
                        loader=self._make_url_loader(source),
                        note=f"🩹 Стикер: {source}",
                    )
                )
                return

        # Прочие неизвестные типы (опросы, геопозиция и так далее).
        note = _describe_unknown(attach, normalized.text)
        if note:
            normalized.notes.append(note)

    def _build(
        self,
        attach: Any,
        kind: AttachmentKind,
        filename: str,
        *,
        size: int | None = None,
        **extra: Any,
    ) -> NormalizedAttachment:
        return NormalizedAttachment(
            kind=kind,
            filename=safe_filename(filename),
            mime_type=None,
            size=size,
            loader=self._make_loader(attach),
            **extra,
        )

    def _make_loader(self, attach: Any) -> Callable[[], Awaitable[bytes]]:
        async def loader() -> bytes:
            payload: bytes | None = None
            try:
                payload, _headers = await self.api.download_file(attach)
            except Exception as error:
                logger.debug("Штатное скачивание не сработало: %s", error, exc_info=True)
            if payload:
                return payload

            # Голосовые и «кружки» pyromax скачивать не умеет: ссылку на них
            # сервер отдаёт под другими ключами, чем для обычного видео.
            payload = await self._download_by_link(attach)
            if payload is None:
                raise ValueError("MAX не отдал содержимое вложения")
            return payload

        return loader

    def _make_url_loader(self, url: str) -> Callable[[], Awaitable[bytes]]:
        """Загрузчик файла по прямой ссылке (стикеры отдаются именно так)."""

        async def loader() -> bytes:
            data = await self._fetch_url(url)
            if data is None:
                raise ValueError("MAX не отдал содержимое вложения")
            return data

        return loader

    async def _download_by_link(self, attach: Any) -> bytes | None:
        """Запросить ссылку на файл напрямую и скачать его.

        pyromax ищет в ответе только ключи качества обычного видео, поэтому для
        голосовых и «кружков» ссылка теряется. Дальше пути расходятся: «кружок»
        отдаётся хранилищем видео как есть, а голосовое там же отвечает 404 —
        его адрес приходит только в медиа-истории чата. Медиа-история для видео
        не годится: она возвращает перекодированный файл, который Telegram уже
        не принимает как видеосообщение.
        """
        # Для голосовых и фотографий адрес файла достаётся из выборки медиа:
        # у первых его нет в push-уведомлении, у вторых — в истории чата.
        media_type = MEDIA_ATTACH_TYPES.get(type(attach))
        if media_type is not None:
            url = await self._media_history_url(attach, media_type)
            if url is not None:
                data = await self._fetch_url(url)
                if data is not None:
                    return data

        for opcode, payload in self._link_requests(attach):
            url = await self._request_file_url(opcode, payload)
            if url is None:
                continue
            data = await self._fetch_url(url)
            if data is not None:
                return data
        return None

    async def _media_history_url(self, attach: Any, attach_type: str) -> str | None:
        """Найти прямую ссылку на вложение в медиа-истории чата.

        Push-уведомление приносит голосовое без адреса файла, а вот выборка
        медиа по чату отдаёт вложения уже со ссылкой — этим и пользуется
        веб-клиент MAX.
        """
        chat_id = getattr(attach, "chat_id", None)
        message_id = getattr(attach, "message_id", None)
        if chat_id is None or message_id is None:
            return None

        target = (
            getattr(attach, "audio_id", None)
            or getattr(attach, "video_id", None)
            or getattr(attach, "photo_id", None)
        )
        payload = {
            "chatId": chat_id,
            "messageId": str(message_id),
            "attachTypes": [attach_type],
            "forward": MEDIA_HISTORY_DEPTH,
            "backward": MEDIA_HISTORY_DEPTH,
        }
        await self._limiter.acquire(int(chat_id))
        try:
            future = await self.api.mapper.protocol.send(
                method=GetFileLinkMethod(opcode=GET_MEDIA_OPCODE, file=_StaticPayload(payload))
            )
            envelope = await future
            answer = envelope.payload or {}
        except Exception:
            logger.warning("Запрос медиа-истории не удался", exc_info=True)
            return None

        url = _find_attach_url(answer, target, str(message_id))
        if url is None:
            logger.info(
                "В медиа-истории нет ссылки на вложение %s (ключи ответа: %s)",
                target,
                sorted(answer),
            )
        return url

    def _link_requests(self, attach: Any) -> list[tuple[int, dict[str, Any]]]:
        """Способы спросить ссылку на файл, от самого вероятного к запасным.

        Запрос с пустым идентификатором MAX не просто отклоняет — он закрывает
        websocket, поэтому такие запросы не отправляются вовсе.
        """
        message_id = getattr(attach, "message_id", None)
        chat_id = getattr(attach, "chat_id", None)
        if message_id is None or chat_id is None:
            # Без адреса сообщения сервер не отвечает, а закрывает соединение.
            return []
        base: dict[str, Any] = {"messageId": message_id, "chatId": chat_id}
        if isinstance(attach, PhotoMappingModel):
            # У фотографии прямой адрес лежит в самом вложении; спрашивать
            # ссылку по websocket нечем и незачем.
            return []
        if isinstance(attach, FileMappingModel):
            if not attach.file_id:
                return []
            return [(Opcode.GET_FILE, {**base, "fileId": attach.file_id})]
        if isinstance(attach, VoiceMappingModel):
            # Способ скачивания голосовых в протоколе MAX не разгадан: сервер
            # отвечает ссылкой на видеосообщение, которого не существует.
            # Оставлены два осмысленных запроса; если оба пусты, вложение
            # заменяется пометкой.
            return [
                (
                    Opcode.GET_VIDEO,
                    {**base, "audioId": attach.audio_id, "token": attach.token},
                ),
                (
                    GET_MEDIA_OPCODE,
                    {**base, "audioId": attach.audio_id, "token": attach.token},
                ),
            ]
        video_id = getattr(attach, "video_id", None)
        token = getattr(attach, "token", None)
        return [
            (Opcode.GET_VIDEO, {**base, "videoId": video_id, "token": token}),
            (Opcode.GET_FILE, {**base, "fileId": video_id}),
        ]

    async def _request_file_url(self, opcode: int, payload: dict[str, Any]) -> str | None:
        """Спросить у MAX ссылку на файл конкретным способом."""
        await self._limiter.acquire(int(payload.get("chatId") or 0))
        try:
            future = await self.api.mapper.protocol.send(
                method=GetFileLinkMethod(opcode=opcode, file=_StaticPayload(payload))
            )
            envelope = await future
            answer = envelope.payload or {}
        except Exception:
            logger.warning("Запрос ссылки (opcode %s) не удался", opcode, exc_info=True)
            return None
        url = _first_url(answer)
        if url is None:
            logger.info("В ответе MAX нет ссылки (opcode %s): ключи %s", opcode, sorted(answer))
        return url

    async def _fetch_url(self, url: str) -> bytes | None:
        """Скачать файл по готовой ссылке."""
        try:
            async with aiohttp.ClientSession(
                headers=DOWNLOAD_HEADERS,
                cookies=AVATAR_COOKIES,
                timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT),
            ) as session:
                async with session.get(url) as response:
                    if response.status != HTTPStatus.OK:
                        logger.info("Файл недоступен (HTTP %s): %s", response.status, url)
                        return None
                    return await read_limited(response, self._settings.max_upload_limit)
        except Exception:
            logger.warning("Не удалось скачать файл по ссылке", exc_info=True)
            return None

    async def _resolve_name(self, user_id: int | None) -> str:
        """Имя пользователя MAX с кэшированием."""
        if user_id is None:
            return "MAX"
        cached = self._names.get(user_id)
        if cached:
            return cached
        name = str(user_id)
        try:
            contact = await self.api.get_user(user_id)
        except Exception:
            logger.debug("Не удалось получить контакт %s", user_id, exc_info=True)
            contact = None
        if contact is not None:
            parts = [part for part in (contact.first_name, contact.last_name) if part]
            if not parts and contact.names:
                parts = [contact.names[0].name or ""]
            name = " ".join(part for part in parts if part) or name
        self._names[user_id] = name
        return name

    async def import_history(self, chat_id: int, limit: int) -> int:
        """Перенести последние сообщения чата MAX в привязанную группу.

        Порядок хронологический — иначе переписка в Telegram окажется вверх
        ногами. Повторы отсекает мост по таблице соответствий, поэтому команду
        можно вызывать повторно без риска задвоить историю.
        """
        if limit <= 0:
            return 0
        try:
            history = await self.api.get_chat_history(chat_id=chat_id, backward=limit)
        except Exception:
            logger.warning("Не удалось получить историю чата %s", chat_id, exc_info=True)
            return 0

        messages = [item for item in history if isinstance(item, Message)]
        messages.sort(key=lambda item: (item.time or 0, str(item.message_id)))
        logger.info("История чата %s: получено %d сообщений", chat_id, len(messages))

        exported = 0
        for message in messages:
            # Уже перенесённое отсеиваем здесь, а не в очереди моста: иначе
            # повторный /sync гоняет сотни сообщений впустую и врёт в отчёте.
            known = await self._storage.find_tg_message(
                chat_id, str(message.message_id), self.account_id
            )
            if known is not None:
                continue
            normalized = await self._normalize(message, is_edit=False, forwarded=False)
            if normalized is None:
                continue
            await self._on_message(normalized)
            exported += 1
            # Без паузы выгрузка сотен сообщений с вложениями заваливает MAX
            # запросами, и сервер закрывает websocket.
            await asyncio.sleep(self._settings.history_import_delay)
        return exported

    # ------------------------------------------------------------------ #
    # Доставка сообщений в MAX
    # ------------------------------------------------------------------ #

    async def deliver(
        self,
        message: NormalizedMessage,
        target_chat_id: int,
        reply_to_target_id: str | None,
    ) -> list[str]:
        """Отправить нормализованное сообщение в чат MAX.

        Автор не подписывается: к MAX подключён единственный аккаунт, и всё,
        что уходит отсюда, отправлено именно им. Подпись нужна лишь в обратную
        сторону, где в одном чате MAX может быть много участников.
        """
        attaches = await self._upload(message, target_chat_id)
        body = "\n".join(part for part in (message.text, *message.notes) if part)

        link = None
        if reply_to_target_id:
            link = MessageLink(type="REPLY", message_id=_as_message_id(reply_to_target_id))

        sent: list[str] = []
        chunks = _split_text(body, self._settings.max_text_limit)

        result = await self._send(
            chat_id=target_chat_id,
            text=chunks[0] if chunks else "",
            spans=message.spans,
            offset=0,
            attaches=attaches,
            link=link,
        )
        if result is not None:
            sent.append(str(result.message_id))

        for chunk in chunks[1:]:
            extra = await self._send(chat_id=target_chat_id, text=chunk, spans=[], offset=0)
            if extra is not None:
                sent.append(str(extra.message_id))
        return sent

    async def edit(
        self,
        message: NormalizedMessage,
        target_chat_id: int,
        target_message_id: str,
    ) -> bool:
        """Отредактировать ранее отправленное сообщение в MAX."""
        body = "\n".join(part for part in (message.text, *message.notes) if part)
        if not body:
            return False
        text = body[: self._settings.max_text_limit]
        try:
            await self._limiter.acquire(target_chat_id)
            await self.api.edit_message(
                chat_id=target_chat_id, message_id=target_message_id, text=text
            )
            return True
        except Exception:
            logger.warning("Не удалось отредактировать сообщение MAX %s", target_message_id)
            return False

    async def send_service(self, chat_id: int, text: str) -> None:
        """Служебное сообщение в чат MAX."""
        await self._send(chat_id=chat_id, text=text, spans=[], offset=0)

    async def _send(
        self,
        *,
        chat_id: int,
        text: str,
        spans: list[TextSpan],
        offset: int,
        attaches: list[Any] | None = None,
        link: MessageLink | None = None,
    ) -> Message | None:
        """Отправить сообщение в MAX с учётом выбранного способа разметки."""
        mode = self._settings.tg_to_max_formatting
        payload_text = text
        elements: list[dict[str, Any]] = []

        if spans and mode != "plain":
            shifted = [
                TextSpan(
                    type=span.type, offset=span.offset + offset, length=span.length, url=span.url
                )
                for span in spans
            ]
            if mode == "elements":
                elements = spans_to_max_elements(shifted)
            else:
                payload_text = spans_to_max_tags(text, shifted)

        for attempt in range(1, SEND_ATTEMPTS + 1):
            await self._limiter.acquire(chat_id)
            try:
                return await self.api(
                    SendMessageMethod,
                    chat_id=chat_id,
                    text=payload_text,
                    attaches=attaches or [],
                    link=link,
                    notify=True,
                    parse_tags=mode == "tags",
                    elements=elements,
                )
            except Exception as error:
                if attempt == SEND_ATTEMPTS or _is_permanent(error):
                    raise
                delay = min(2.0 * 2 ** (attempt - 1), 20.0)
                logger.warning(
                    "Повтор отправки в MAX (%s из %s) через %.0f с: %s",
                    attempt,
                    SEND_ATTEMPTS,
                    delay,
                    error,
                )
                await asyncio.sleep(delay)
        return None

    async def _upload(self, message: NormalizedMessage, chat_id: int) -> list[Any]:
        """Скачать вложения из источника и залить их в MAX."""
        uploaded: list[Any] = []
        for attachment in message.attachments:
            if attachment.size is not None and attachment.size > self._settings.max_upload_limit:
                message.notes.append(
                    f"⚠️ «{attachment.filename}» ({human_size(attachment.size)}) "
                    "слишком большой для MAX"
                )
                continue
            try:
                payload = await attachment.loader()
            except Exception as error:
                logger.warning("Не удалось получить вложение %s: %s", attachment.filename, error)
                message.notes.append(f"⚠️ «{attachment.filename}» не перенесено: {error}")
                continue

            kind = attachment.kind
            filename = attachment.filename
            if kind is AttachmentKind.STICKER:
                converted = webp_to_png(payload)
                if converted is not None:
                    payload = converted
                    filename = filename.rsplit(".", 1)[0] + ".png"
                    kind = AttachmentKind.PHOTO
                elif attachment.note:
                    message.notes.append(f"🩹 Стикер {attachment.note}")

            hint = DEGRADED_NOTES.get(kind)
            if hint:
                # Получателю в MAX иначе непонятно, чем этот файл был в Telegram.
                message.notes.append(hint)

            typeof = UPLOAD_TYPES.get(kind, FileAttachment)
            try:
                await self._limiter.acquire(chat_id)
                result = await self.api.upload_file(data=payload, typeof=typeof, file_name=filename)
            except Exception as error:
                logger.warning("Не удалось загрузить %s в MAX: %s", filename, error)
                message.notes.append(f"⚠️ «{filename}» не загружено в MAX: {error}")
                continue
            uploaded.extend(result)
        return uploaded

    # ------------------------------------------------------------------ #
    # Справочник чатов (RemoteDirectory)
    # ------------------------------------------------------------------ #

    async def list_chats(self, query: str | None = None) -> list[RemoteChat]:
        """Список чатов MAX, доступных аккаунту."""
        await self._refresh_chats()
        chats = list(self._chat_cache.values())
        if query:
            needle = query.casefold()
            chats = [
                chat for chat in chats if needle in chat.title.casefold() or needle in str(chat.id)
            ]
        return chats

    async def resolve_chat(self, chat_id: int) -> RemoteChat | None:
        """Найти чат MAX по идентификатору."""
        if chat_id in self._chat_cache:
            return self._chat_cache[chat_id]
        try:
            chat = await self.api.get_chat(chat_id)
        except Exception:
            logger.debug("Чат MAX %s не найден", chat_id, exc_info=True)
            return None
        remote = await self._to_remote(chat)
        self._chat_cache[chat_id] = remote
        return remote

    async def _refresh_chats(self) -> None:
        """Обновить кэш чатов."""
        chats: list[Any] = []
        try:
            chats = await self.api.fetch_chats()
        except Exception:
            logger.debug("fetch_chats не сработал, используем список из авторизации", exc_info=True)
        if not chats:
            chats = list(self.api.chats or [])
        for chat in chats:
            # id может быть нулевым: так MAX обозначает «Избранное».
            if getattr(chat, "id", None) is None:
                logger.debug("Пропускаем запись чата без идентификатора: %s", chat)
                continue
            self._chat_cache[chat.id] = await self._to_remote(chat)

    async def _to_remote(self, chat: Any) -> RemoteChat:
        """Привести чат MAX к краткому описанию."""
        title = chat.title
        partner = next(
            (
                participant
                for participant in (chat.participants or {})
                if self._api is None or participant != self._api.id
            ),
            None,
        )
        if not title and chat.type == "DIALOG" and partner is not None:
            title = await self._resolve_name(int(partner))
        last = chat.last_message.text if chat.last_message else None
        icon = chat.base_icon_url or chat.base_raw_icon_url
        if not icon and chat.type == "DIALOG" and partner is not None:
            icon = await self._resolve_avatar(int(partner))
        if not icon and chat.id == FAVORITES_CHAT_ID and self._api is not None:
            # У «Избранного» нет ни своей картинки, ни собеседника: это чат с
            # самим собой, поэтому его лицо — собственный аватар владельца.
            icon = await self._resolve_avatar(int(self._api.id or 0))
        if not title and chat.id == FAVORITES_CHAT_ID:
            title = "Избранное"
        return RemoteChat(
            id=chat.id,
            title=title or f"Чат {chat.id}",
            type=chat.type,
            participants_count=chat.participants_count or None,
            last_message=last,
            icon_url=icon,
        )

    async def _resolve_avatar(self, user_id: int) -> str | None:
        """Аватар собеседника: у личных чатов своей картинки нет."""
        try:
            contact = await self.api.get_user(user_id)
        except Exception:
            logger.debug("Не удалось получить контакт %s", user_id, exc_info=True)
            return None
        if contact is None:
            return None
        return contact.avatar_url or contact.raw_avatar_url

    async def fetch_avatar(self, chat: RemoteChat) -> bytes | None:
        """Скачать картинку чата MAX для аватара группы Telegram.

        Хранилище картинок MAX отдаёт файл только знакомому клиенту, поэтому
        запрос идёт с теми же заголовками, что и загрузка вложений.
        """
        if not chat.icon_url:
            return None
        content_type = ""
        try:
            async with aiohttp.ClientSession(
                headers=AVATAR_HEADERS,
                cookies=AVATAR_COOKIES,
                timeout=aiohttp.ClientTimeout(total=AVATAR_TIMEOUT),
            ) as session:
                async with session.get(chat.icon_url) as response:
                    if response.status != HTTPStatus.OK:
                        logger.warning(
                            "Картинка чата %s недоступна (HTTP %s): %s",
                            chat.id,
                            response.status,
                            chat.icon_url,
                        )
                        return None
                    content_type = response.headers.get("Content-Type", "")
                    data = await read_limited(response, AVATAR_SIZE_LIMIT)
        except Exception:
            logger.warning("Не удалось скачать картинку чата %s", chat.id, exc_info=True)
            return None
        if data is None:
            logger.warning("Картинка чата %s больше %s байт", chat.id, AVATAR_SIZE_LIMIT)
            return None

        photo = to_telegram_photo(data)
        if photo is None:
            logger.warning(
                "По адресу картинки чата %s пришло не изображение (тип %r, %d байт, начало %s): %s",
                chat.id,
                content_type,
                len(data),
                data[:16].hex(),
                chat.icon_url,
            )
        return photo


class _StaticPayload:
    """Готовый payload запроса вместо объекта вложения.

    ``GetFileLinkMethod`` берёт тело запроса из свойства вложения, а нам нужно
    подставить собственный набор полей.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self.get_payload_to_get_link = payload


def _find_attach_url(payload: Any, target_id: Any, message_id: str) -> str | None:
    """Достать из ответа ссылку на нужное вложение.

    Структура ответа не описана в pyromax, поэтому дерево обходится по всем
    словарям: берётся вложение с совпадающим идентификатором и полем url.
    """
    fallback: str | None = None
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            url = node.get("url")
            if isinstance(url, str) and url.startswith("http"):
                identifiers = {
                    node.get("audioId"),
                    node.get("videoId"),
                    node.get("fileId"),
                }
                if target_id is not None and target_id in identifiers:
                    return url
                if str(node.get("messageId") or "") == message_id:
                    return url
                fallback = fallback or url
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return fallback


def _first_url(payload: dict[str, Any]) -> str | None:
    """Найти в ответе первую пригодную ссылку на файл.

    Ключ зависит от типа вложения (качество видео, ссылка на файл, поток
    голосового), поэтому берётся первое значение, похожее на адрес.
    """
    for value in payload.values():
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def _save_qr_image(code: Any) -> Path:
    """Сохранить QR-код авторизации картинкой (блокирующая часть)."""
    path = Path("max_login_qr.png").resolve()
    code.make_image().save(path)
    return path


def _as_message_id(value: str) -> int | str:
    """Идентификаторы сообщений MAX числовые, но приходят строкой."""
    try:
        return int(value)
    except ValueError:
        return value


def _describe_unknown(attach: Any, text: str = "") -> str:
    """Описать вложение, для которого нет прямого соответствия."""
    if isinstance(attach, dict):
        kind = str(attach.get("_type") or attach.get("type") or "UNKNOWN")
        if kind == "STICKER":
            url = attach.get("url") or attach.get("baseUrl")
            return f"🩹 Стикер{f': {url}' if url else ''}"
        if kind == "LOCATION":
            latitude = attach.get("latitude")
            longitude = attach.get("longitude")
            if latitude is not None and longitude is not None:
                return (
                    f"📍 Геопозиция: {latitude}, {longitude}\n"
                    f"https://maps.google.com/?q={latitude},{longitude}"
                )
        if kind == "CONTACT":
            return f"👤 Контакт: {attach.get('name') or attach.get('contactId') or ''}".strip()
        if kind == "SHARE":
            # Превью ссылки: сама ссылка уже есть в тексте сообщения, поэтому
            # выводятся только заголовок и описание, и то если они добавляют смысл.
            return _describe_share(
                attach.get("url"), attach.get("title"), attach.get("description"), text
            )
        return f"📎 Вложение MAX типа {kind} не поддерживается мостом"
    return f"📎 Вложение MAX ({type(attach).__name__}) не поддерживается мостом"


def _first_link(attach: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Первая пригодная ссылка среди указанных полей вложения."""
    for key in keys:
        value = attach.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def _describe_share(url: str | None, title: str | None, description: str | None, text: str) -> str:
    """Описать превью ссылки, не дублируя то, что уже есть в тексте."""
    parts = [part.strip() for part in (title, description) if part and part.strip()]
    if url and url not in text:
        parts.append(url)
    if not parts:
        return ""
    return "🔗 " + "\n".join(parts)


def _split_text(text: str, limit: int) -> list[str]:
    """Разбить текст на части по лимиту MAX."""
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


def _is_permanent(error: BaseException) -> bool:
    """Отказ, который не исправится повтором.

    MAX отвечает одинаково хоть пять раз подряд, если не принял само вложение,
    — повторы только задерживают очередь и путают журнал.
    """
    text = str(error)
    return "errors.process.attachment" in text or "not.supported" in text


def _restore_context(attach: Any, message: Message) -> None:
    """Дописать вложению чат и сообщение, из которых оно приехало.

    В истории чата MAX отдаёт вложения без этих полей, а запрос ссылки без
    них сервер не отклоняет, а обрывает websocket — уже наблюдали.
    """
    for field, value in (("chat_id", message.chat_id), ("message_id", message.message_id)):
        if getattr(attach, field, None) is None and value is not None:
            with suppress(Exception):
                setattr(attach, field, value)
