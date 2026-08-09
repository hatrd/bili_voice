from __future__ import annotations

import unittest
from unittest.mock import patch

from bilibili_api.login_v2 import QrCodeLoginChannel, QrCodeLoginEvents

from backend.auth import AuthManager


class _Picture:
    content = b"png"


class _Credential:
    def __init__(self, cookies: dict[str, str]) -> None:
        self._cookies = cookies

    def get_cookies(self) -> dict[str, str]:
        return self._cookies


class _QrLogin:
    def __init__(self, state: QrCodeLoginEvents, credential: _Credential) -> None:
        self._state = state
        self._credential = credential

    async def generate_qrcode(self) -> None:
        return None

    def get_qrcode_picture(self) -> _Picture:
        return _Picture()

    async def check_state(self) -> QrCodeLoginEvents:
        return self._state

    def get_credential(self) -> _Credential:
        return self._credential


class AuthManagerQrTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_qr_uses_tv_channel(self) -> None:
        manager = AuthManager()
        qr = _QrLogin(QrCodeLoginEvents.SCAN, _Credential({}))

        with patch("backend.auth.QrCodeLogin", return_value=qr) as qr_class:
            token, encoded = await manager.start_qr()

        self.assertTrue(token)
        self.assertEqual(encoded, "cG5n")
        qr_class.assert_called_once_with(platform=QrCodeLoginChannel.TV)

    async def test_done_is_reported_after_valid_credentials_are_saved(self) -> None:
        manager = AuthManager()
        token, session = await manager._ensure_session()
        session.qr = _QrLogin(
            QrCodeLoginEvents.DONE,
            _Credential({"SESSDATA": "session", "bili_jct": "csrf"}),
        )

        with patch("backend.auth.save_credential") as save:
            state, done = await manager.check_qr(token)

        self.assertEqual(state, QrCodeLoginEvents.DONE)
        self.assertTrue(done)
        self.assertTrue(session.cookies_saved)
        saved = save.call_args.args[0]
        self.assertEqual(saved.sessdata, "session")
        self.assertEqual(saved.bili_jct, "csrf")

    async def test_empty_credentials_do_not_report_done(self) -> None:
        manager = AuthManager()
        token, session = await manager._ensure_session()
        session.qr = _QrLogin(
            QrCodeLoginEvents.DONE,
            _Credential({"ac_time_value": "refresh-only"}),
        )

        with patch("backend.auth.save_credential") as save:
            with self.assertRaisesRegex(RuntimeError, "did not return"):
                await manager.check_qr(token)

        save.assert_not_called()
        self.assertFalse(session.qr_done)
        self.assertFalse(session.cookies_saved)


if __name__ == "__main__":
    unittest.main()
