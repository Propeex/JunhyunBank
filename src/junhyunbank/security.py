from __future__ import annotations

import keyring


SERVICE_NAME = "JunhyunBank.Upbit"
ACCESS_NAME = "access_key"
SECRET_NAME = "secret_key"


class KeyStore:
    def save(self, access_key: str, secret_key: str) -> None:
        access_key = access_key.strip()
        secret_key = secret_key.strip()
        if not access_key or not secret_key:
            raise ValueError("Access Key와 Secret Key를 모두 입력해야 합니다.")
        keyring.set_password(SERVICE_NAME, ACCESS_NAME, access_key)
        keyring.set_password(SERVICE_NAME, SECRET_NAME, secret_key)

    def load(self) -> tuple[str | None, str | None]:
        return (
            keyring.get_password(SERVICE_NAME, ACCESS_NAME),
            keyring.get_password(SERVICE_NAME, SECRET_NAME),
        )

    def clear(self) -> None:
        for name in (ACCESS_NAME, SECRET_NAME):
            try:
                keyring.delete_password(SERVICE_NAME, name)
            except keyring.errors.PasswordDeleteError:
                pass

    def exists(self) -> bool:
        access, secret = self.load()
        return bool(access and secret)
