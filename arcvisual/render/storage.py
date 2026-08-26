"""R2 upload and the derived assets each scene ships with.

Keys are content-addressed (see :func:`arcvisual.cache.hashing.r2_keys`), so an
upload is idempotent and two papers that produce an identical scene share one
object. The bucket itself stays private; readers only ever reach objects through
the CDN domain, which is why ``public_url`` refuses to fall back to an R2
endpoint when ``R2_PUBLIC_BASE`` is unset — a silent fallback would quietly
expose the origin.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from arcvisual.config import settings

log = logging.getLogger(__name__)


@dataclass
class DerivedAssets:
    mp4: Path
    webm: Path | None
    poster: Path
    framestrip: Path
    duration_s: float


def ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def derive_assets(mp4: Path, outdir: Path, *, keyframes: int = 8) -> DerivedAssets:
    """Produce the poster, the WebM fallback and Gate 4's contact sheet.

    The contact sheet is a single image rather than N files because Gate 4 sends
    it to a vision model as one attachment; N images would cost N times the
    per-image overhead for the same information.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    poster = outdir / "poster.png"
    framestrip = outdir / "framestrip.png"
    webm = outdir / "scene.webm"
    duration = probe_duration(mp4)

    exe = ffmpeg()
    if exe is None:
        log.warning("ffmpeg not found; shipping the mp4 without derived assets")
        return DerivedAssets(
            mp4=mp4, webm=None, poster=poster, framestrip=framestrip, duration_s=duration
        )

    # Poster: a frame ~15% in. Frame zero of a progressive-disclosure scene is an
    # empty canvas, which makes a useless thumbnail.
    _run(
        [
            exe,
            "-y",
            "-ss",
            f"{max(0.1, duration * 0.15):.2f}",
            "-i",
            str(mp4),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(poster),
        ]
    )

    # Contact sheet for Gate 4: evenly spaced keyframes tiled into one image.
    cols = 3
    rows = max(1, (keyframes + cols - 1) // cols)
    fps = keyframes / duration if duration > 0 else 1.0
    _run(
        [
            exe,
            "-y",
            "-i",
            str(mp4),
            "-vf",
            f"fps={fps:.4f},scale=480:-1,tile={cols}x{rows}",
            "-frames:v",
            "1",
            str(framestrip),
        ]
    )

    # VP9 fallback for browsers without the H.264 profile we ship.
    ok = _run(
        [
            exe,
            "-y",
            "-i",
            str(mp4),
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "0",
            "-crf",
            "34",
            "-row-mt",
            "1",
            "-an",
            str(webm),
        ]
    )

    return DerivedAssets(
        mp4=mp4,
        webm=webm if ok and webm.exists() else None,
        poster=poster,
        framestrip=framestrip,
        duration_s=duration,
    )


def transcode_all_keyframe(mp4: Path, out: Path) -> Path:
    """Re-encode with every frame a keyframe, for cheap ``currentTime`` seeking.

    Only worth the file-size cost on scenes flagged ``scrubbable`` — an
    all-keyframe encode is several times larger, and autoplay-on-enter (the
    default playback mode) never seeks.
    """
    exe = ffmpeg()
    if exe is None:
        return mp4
    ok = _run(
        [
            exe,
            "-y",
            "-i",
            str(mp4),
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "20",
            "-g",
            "1",
            "-keyint_min",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-an",
            str(out),
        ]
    )
    return out if ok and out.exists() else mp4


def probe_duration(path: Path) -> float:
    probe = shutil.which("ffprobe")
    if probe is None:
        return 0.0
    try:
        proc = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float((proc.stdout or "0").strip() or 0.0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def _run(cmd: list[str]) -> bool:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("ffmpeg step failed: %s", exc)
        return False
    if proc.returncode != 0:
        log.warning("ffmpeg exited %s: %s", proc.returncode, proc.stderr[-400:])
        return False
    return True


# --------------------------------------------------------------------------- #
# R2
# --------------------------------------------------------------------------- #

_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".png": "image/png",
    ".json": "application/json",
}


def r2_client():
    """A boto3 S3 client pointed at R2. Raises if credentials are absent."""
    import boto3

    cfg = settings()
    if not (cfg.r2_account_id and cfg.r2_access_key_id):
        raise RuntimeError("R2 credentials are not configured")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{cfg.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=cfg.r2_access_key_id,
        aws_secret_access_key=cfg.r2_secret_access_key,
        region_name="auto",
    )


def upload(path: Path, key: str, *, client=None) -> int:
    """Upload one object, returning its size. Idempotent by content-addressed key."""
    cfg = settings()
    client = client or r2_client()
    content_type = _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
    client.upload_file(
        str(path),
        cfg.r2_bucket,
        key,
        ExtraArgs={
            "ContentType": content_type,
            # Content-addressed keys can never change meaning, so they are
            # immutable for a year at the edge.
            "CacheControl": "public, max-age=31536000, immutable",
        },
    )
    return path.stat().st_size


def upload_scene(
    assets: DerivedAssets, keys: dict[str, str], *, client=None
) -> dict[str, int]:
    """Upload a scene's artifacts. Missing optional assets are skipped, not faked."""
    client = client or r2_client()
    sizes: dict[str, int] = {}
    for name, path in (
        ("mp4_key", assets.mp4),
        ("webm_key", assets.webm),
        ("poster_key", assets.poster),
        ("framestrip_key", assets.framestrip),
    ):
        if path is None or not path.exists() or name not in keys:
            continue
        sizes[name] = upload(path, keys[name], client=client)
    return sizes


def public_url(key: str) -> str:
    """The reader-facing URL. Refuses to expose the R2 origin."""
    base = settings().r2_public_base.rstrip("/")
    if not base:
        raise RuntimeError(
            "R2_PUBLIC_BASE (the CDN domain) is not set. The R2 bucket is private "
            "and must not be served directly."
        )
    return f"{base}/{key.lstrip('/')}"


# --------------------------------------------------------------------------- #
# Local artifact store
# --------------------------------------------------------------------------- #
#
# R2 is what production serves from. This is the same shape on disk, so a
# deployment with no cloud credentials still *works* rather than rendering videos
# into a temp directory and serving empty players. That was the actual state
# before this existed: the pipeline rendered five scenes, recorded five content
# hashes, and the article showed five black boxes.
#
# Content-addressed with the same key layout as R2, so switching between them is
# a configuration change and never a data migration.


def local_media_root() -> Path:
    """Where local artifacts live. Content-addressed, same key layout as R2."""
    import os

    return Path(os.environ.get("ARCVISUAL_MEDIA_DIR", "./arcvisual-media")).resolve()


def has_scene_local(key: str) -> bool:
    """Whether the bytes for a stored key are actually on disk.

    The cache check needs this because a database row is not evidence: an earlier bug
    recorded artifacts with zero bytes, and a row-only check would have served empty
    players out of the cache forever.
    """
    if not key:
        return False
    path = local_media_root() / key
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def save_scene_local(
    mp4: Path | None,
    keys: dict[str, str],
    *,
    assets: DerivedAssets | None = None,
    root: Path | None = None,
) -> dict[str, int]:
    """Copy a rendered scene into the local store under its content-addressed keys.

    Returns ``{key: bytes}`` for what was actually written. Missing inputs are
    skipped rather than raising: a poster that failed to derive should not cost a
    scene its video.
    """
    import shutil

    root = root or local_media_root()
    written: dict[str, int] = {}

    def put(src: Path | None, key: str | None) -> None:
        if not src or not key or not Path(src).exists():
            return
        dest = root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        written[key] = dest.stat().st_size

    put(mp4, keys.get("mp4_key"))
    if assets is not None:
        put(assets.webm, keys.get("webm_key"))
        put(assets.poster, keys.get("poster_key"))
        put(assets.framestrip, keys.get("framestrip_key"))
    return written


def media_base_url() -> str:
    """What the reader should prefix media keys with.

    The CDN in production; the API's own media route otherwise. Never the R2
    endpoint directly — that bucket is private, and a silent fallback to it would
    hand out URLs that 403.
    """
    configured = settings().r2_public_base
    if configured:
        return configured.rstrip("/")
    return "/api/media"
