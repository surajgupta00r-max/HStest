from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable

MAX_COOKIE_FILE_BYTES = 2 * 1024 * 1024


class CookieValidationError(ValueError):
    """A user-uploaded cookie file is malformed or has no supported-domain cookies."""


class CookieStore:
    """Stores one filtered Netscape-format cookie file per Telegram user.

    Cookie contents are never written to the database or logs. Files are stored with
    owner-only permissions on POSIX systems. A server-side COOKIE_FILE may be used as
    a fallback when a user has not uploaded their own file.
    """

    def __init__(self, root: Path, global_cookie_file: Path | None, allowed_domains: Iterable[str]):
        self.root = Path(root).expanduser().resolve()
        self.global_cookie_file = Path(global_cookie_file).expanduser() if global_cookie_file else None
        self.allowed_domains = tuple(
            domain.strip().lower().lstrip(".").rstrip(".")
            for domain in allowed_domains if domain and domain.strip()
        )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._chmod(self.root, 0o700)

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except OSError:
            # Some filesystems/platforms do not support POSIX permissions.
            pass

    def path_for_user(self, user_id: int) -> Path:
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError("Invalid Telegram user ID.")
        return self.root / f"user_{user_id}.cookies.txt"

    def has_user_cookie(self, user_id: int) -> bool:
        path = self.path_for_user(user_id)
        return path.is_file() and path.stat().st_size > 0

    def has_global_cookie(self) -> bool:
        return bool(self.global_cookie_file and self.global_cookie_file.is_file())

    def get_for_user(self, user_id: int | None) -> Path | None:
        if user_id is not None:
            path = self.path_for_user(user_id)
            if path.is_file() and path.stat().st_size > 0:
                return path
        if self.global_cookie_file and self.global_cookie_file.is_file():
            return self.global_cookie_file
        return None

    def status_for_user(self, user_id: int) -> str:
        if self.has_user_cookie(user_id):
            return "Your uploaded cookies"
        if self.has_global_cookie():
            return "Server default cookies"
        return "Not configured"

    def _domain_is_allowed(self, cookie_domain: str) -> bool:
        domain = cookie_domain.lower().lstrip(".").rstrip(".")
        if domain.startswith("#httponly_"):
            domain = domain[len("#httponly_"):].lstrip(".")
        if not domain or any(ch in domain for ch in "/\\ \t"):
            return False
        return any(domain == allowed or domain.endswith("." + allowed) for allowed in self.allowed_domains)

    def _filter_content(self, raw: bytes) -> tuple[bytes, int]:
        if not raw:
            raise CookieValidationError("The uploaded file is empty.")
        if len(raw) > MAX_COOKIE_FILE_BYTES:
            raise CookieValidationError("Cookie files must be 2 MB or smaller.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CookieValidationError("The file must be a UTF-8 Netscape-format cookies.txt export.") from exc
        if "\x00" in text:
            raise CookieValidationError("The file is not a valid text cookies file.")

        accepted: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            # Netscape cookies use # comments; #HttpOnly_ is a valid cookie row marker.
            if line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            parts = line.split("\t")
            if len(parts) != 7:
                continue
            domain_field = parts[0]
            domain_for_check = domain_field
            if domain_for_check.startswith("#HttpOnly_"):
                domain_for_check = domain_for_check[len("#HttpOnly_"):]
            include_subdomains, path, secure, expires, name, _value = parts[1:]
            if not self._domain_is_allowed(domain_for_check):
                continue
            if include_subdomains.upper() not in {"TRUE", "FALSE"}:
                continue
            if not path.startswith("/") or secure.upper() not in {"TRUE", "FALSE"}:
                continue
            if not name:
                continue
            try:
                int(expires)
            except ValueError:
                continue
            accepted.append(line)

        if not accepted:
            raise CookieValidationError(
                "No valid Hotstar/SonyLIV cookies were found. Upload a Netscape-format cookies.txt "
                "export containing cookies for a supported Hotstar or SonyLIV domain."
            )
        output = "# Netscape HTTP Cookie File\n" + "\n".join(accepted) + "\n"
        return output.encode("utf-8"), len(accepted)

    def save_bytes(self, user_id: int, raw: bytes) -> int:
        filtered, count = self._filter_content(raw)
        target = self.path_for_user(user_id)
        fd, temp_name = tempfile.mkstemp(prefix=f".user_{int(user_id)}_", suffix=".tmp", dir=self.root)
        temp = Path(temp_name)
        try:
            self._chmod(temp, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(filtered)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
            self._chmod(target, 0o600)
            self._chmod(self.root, 0o700)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        return count

    def save_file(self, user_id: int, input_path: Path) -> int:
        input_path = Path(input_path)
        try:
            size = input_path.stat().st_size
        except OSError as exc:
            raise CookieValidationError("The uploaded file could not be read.") from exc
        if size > MAX_COOKIE_FILE_BYTES:
            raise CookieValidationError("Cookie files must be 2 MB or smaller.")
        try:
            raw = input_path.read_bytes()
        except OSError as exc:
            raise CookieValidationError("The uploaded file could not be read.") from exc
        return self.save_bytes(user_id, raw)

    def remove_user_cookie(self, user_id: int) -> bool:
        path = self.path_for_user(user_id)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
