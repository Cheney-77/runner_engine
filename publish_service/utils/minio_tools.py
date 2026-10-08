from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error


class MinioToolError(RuntimeError):
    """Base exception for MinIO helper operations."""


class MinioConfigurationError(MinioToolError):
    """MinIO environment configuration is invalid."""


class MinioAccessDeniedError(MinioToolError):
    """MinIO rejected the request because of permissions."""


class MinioBucketNotFoundError(MinioToolError):
    """Requested bucket does not exist."""


class MinioFolderNotFoundError(MinioToolError):
    """Requested prefix contains no downloadable objects."""


@dataclass(frozen=True)
class MinioFolderDownloadResult:
    bucket: str
    folder_path: str
    workspace_name: str
    local_path: Path
    downloaded_files: int
    downloaded_bytes: int

    def to_dict(self) -> dict:
        return {
            "bucket": self.bucket,
            "folder_path": self.folder_path,
            "workspace_name": self.workspace_name,
            "local_path": str(self.local_path),
            "downloaded_files": self.downloaded_files,
            "downloaded_bytes": self.downloaded_bytes,
        }


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()

    if not value:
        raise MinioConfigurationError(
            f"{name} is required"
        )

    return value


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def create_minio_client() -> Minio:
    """
    Create MinIO client from environment variables.

    Supported configuration:

        MINIO_ENDPOINT=minio.example.com:9000
        MINIO_ACCESS_KEY=xxx
        MINIO_SECRET_KEY=xxx
        MINIO_SECURE=true

    Also supports:

        MINIO_ENDPOINT=https://minio.example.com
    """

    endpoint_raw = _required_env("MINIO_ENDPOINT")
    access_key = _required_env("MINIO_ACCESS_KEY")
    secret_key = _required_env("MINIO_SECRET_KEY")

    secure_env = os.environ.get(
        "MINIO_SECURE",
        "true",
    )

    # 同时兼容：
    #
    # minio.example.com:9000
    #
    # 和：
    #
    # https://minio.example.com
    #
    if "://" in endpoint_raw:
        parsed = urlparse(endpoint_raw)

        if parsed.scheme not in {"http", "https"}:
            raise MinioConfigurationError(
                "MINIO_ENDPOINT scheme must be http or https"
            )

        if not parsed.netloc:
            raise MinioConfigurationError(
                "Invalid MINIO_ENDPOINT"
            )

        endpoint = parsed.netloc
        secure = parsed.scheme == "https"

    else:
        endpoint = endpoint_raw.rstrip("/")
        secure = _parse_bool(secure_env)

    return Minio(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )


def normalize_folder_path(folder_path: str) -> str:
    """
    Normalize:

        storage/user-1/flow
        /storage/user-1/flow/
        storage/user-1/flow/

    into:

        storage/user-1/flow/
    """

    folder_path = folder_path.strip()

    if not folder_path:
        raise ValueError(
            "folder_path cannot be empty"
        )

    # MinIO / S3 object key 按 / 分层，
    # 不接受反斜杠形式，避免本地路径语义混乱。
    if "\\" in folder_path:
        raise ValueError(
            "folder_path must use '/' as path separator"
        )

    folder_path = folder_path.lstrip("/")

    parts = [
        part
        for part in folder_path.split("/")
        if part not in {"", "."}
    ]

    if not parts:
        raise ValueError(
            "folder_path cannot be empty"
        )

    if ".." in parts:
        raise ValueError(
            "folder_path cannot contain '..'"
        )

    return "/".join(parts) + "/"


def _sanitize_workspace_name(name: str) -> str:
    """
    The final MinIO folder name becomes a local Linux directory name.

    Preserve Unicode names while replacing control characters and
    path separators.
    """

    sanitized = "".join(
        "_"
        if char in {"/", "\\"} or ord(char) < 32
        else char
        for char in name
    ).strip()

    if not sanitized or sanitized in {".", ".."}:
        raise ValueError(
            f"Invalid workspace folder name: {name!r}"
        )

    return sanitized


def _create_workspace_dir(
    workspace_root: Path,
    folder_path: str,
) -> Path:
    """
    storage/user-1/flow/
                  ↓
    <workspace_root>/flow_202609181223
    """

    workspace_root = workspace_root.expanduser().resolve()

    workspace_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    folder_name = folder_path.rstrip("/").split("/")[-1]
    folder_name = _sanitize_workspace_name(folder_name)

    # 按用户要求：
    # flow_202609181223
    timestamp = datetime.now().strftime("%Y%m%d%H%M")

    target_dir = (
        workspace_root
        / f"{folder_name}_{timestamp}"
    )

    try:
        target_dir.mkdir(
            parents=False,
            exist_ok=False,
        )

        return target_dir

    except FileExistsError:
        # 同一分钟内可能出现多个相同 flow 下载请求。
        # 冲突时增加秒和微秒，避免覆盖已有 workspace。
        timestamp = datetime.now().strftime(
            "%Y%m%d%H%M%S%f"
        )

        target_dir = (
            workspace_root
            / f"{folder_name}_{timestamp}"
        )

        target_dir.mkdir(
            parents=False,
            exist_ok=False,
        )

        return target_dir


def _safe_local_path(
    workspace_dir: Path,
    relative_path: str,
) -> Path:
    """
    Convert MinIO relative object key into a safe local path.

    Prevents object names such as:

        ../../etc/passwd

    from escaping workspace_dir.
    """

    relative = PurePosixPath(relative_path)

    if relative.is_absolute():
        raise MinioToolError(
            f"Unsafe MinIO object path: {relative_path}"
        )

    if ".." in relative.parts:
        raise MinioToolError(
            f"Unsafe MinIO object path: {relative_path}"
        )

    base = workspace_dir.resolve()

    target = (
        base.joinpath(*relative.parts)
        .resolve()
    )

    try:
        target.relative_to(base)
    except ValueError as exc:
        raise MinioToolError(
            f"Unsafe MinIO object path: {relative_path}"
        ) from exc

    return target


def _raise_minio_error(exc: S3Error) -> None:
    if exc.code in {
        "NoSuchBucket",
        "NoSuchBucketPolicy",
    }:
        raise MinioBucketNotFoundError(
            exc.message or exc.code
        ) from exc

    if exc.code in {
        "AccessDenied",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
    }:
        raise MinioAccessDeniedError(
            f"{exc.code}: {exc.message}"
        ) from exc

    raise MinioToolError(
        f"MinIO error {exc.code}: {exc.message}"
    ) from exc


def download_minio_folder(
    *,
    bucket: str,
    folder_path: str,
    workspace_root: Path,
    client: Minio | None = None,
) -> MinioFolderDownloadResult:
    """
    Recursively download all objects under a MinIO prefix.

    Example:

        bucket:
            my-bucket

        folder_path:
            storage/user-1/flow

    MinIO:

        storage/user-1/flow/a.txt
        storage/user-1/flow/sub/b.txt

    Local:

        <workspace_root>/flow_202609181223/
            a.txt
            sub/b.txt

    If any download fails, the incomplete workspace directory
    will be removed.
    """

    bucket = bucket.strip()

    if not bucket:
        raise ValueError(
            "bucket cannot be empty"
        )

    folder_path = normalize_folder_path(
        folder_path
    )

    minio_client = client or create_minio_client()

    workspace_dir = _create_workspace_dir(
        workspace_root,
        folder_path,
    )

    downloaded_files = 0
    downloaded_bytes = 0
    discovered_objects = 0

    try:
        try:
            objects = minio_client.list_objects(
                bucket_name=bucket,
                prefix=folder_path,
                recursive=True,
            )

            for obj in objects:
                discovered_objects += 1

                object_name = obj.object_name

                if not object_name.startswith(folder_path):
                    continue

                relative_path = object_name[
                    len(folder_path):
                ]

                if not relative_path:
                    continue

                # MinIO 有可能存在：
                #
                # storage/user-1/flow/subdir/
                #
                # 这种 0-byte 目录占位对象。
                if object_name.endswith("/"):
                    local_dir = _safe_local_path(
                        workspace_dir,
                        relative_path,
                    )

                    local_dir.mkdir(
                        parents=True,
                        exist_ok=True,
                    )

                    continue

                local_file = _safe_local_path(
                    workspace_dir,
                    relative_path,
                )

                local_file.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                # MinIO SDK 直接流式写本地磁盘。
                # 不会将整个文件读入 Python 内存。
                minio_client.fget_object(
                    bucket_name=bucket,
                    object_name=object_name,
                    file_path=str(local_file),
                )

                downloaded_files += 1
                downloaded_bytes += (
                    local_file.stat().st_size
                )

        except S3Error as exc:
            _raise_minio_error(exc)

        if downloaded_files == 0:
            raise MinioFolderNotFoundError(
                "No files found under "
                f"bucket={bucket}, "
                f"folder_path={folder_path}"
            )

        return MinioFolderDownloadResult(
            bucket=bucket,
            folder_path=folder_path,
            workspace_name=workspace_dir.name,
            local_path=workspace_dir,
            downloaded_files=downloaded_files,
            downloaded_bytes=downloaded_bytes,
        )

    except Exception:
        # 下载失败后不要保留半成品 workspace。
        shutil.rmtree(
            workspace_dir,
            ignore_errors=True,
        )

        raise