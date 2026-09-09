import asyncio
import io
import tarfile

from repowatch.config import RepoConfig
from repowatch.parsers.apk import ApkParser

APKINDEX_SAMPLE = (
    "P:openssl\nV:3.3.2-r0\nA:x86_64\n"
    "\n"
    "P:musl\nV:1.2.5-r0\nA:x86_64\n"
    "\n"
)


def _make_fake_apkindex_tar_gz(text: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = text.encode("utf-8")
        info = tarfile.TarInfo(name="APKINDEX")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_apk_parser_parses_sample(monkeypatch):
    repo = RepoConfig(
        id="alpine-test",
        type="apk",
        upstream="https://example.org/alpine/v3.20/main",
        arch="x86_64",
    )
    parser = ApkParser(repo)

    fake_bytes = _make_fake_apkindex_tar_gz(APKINDEX_SAMPLE)

    async def fake_http_get(client, url):
        return fake_bytes

    monkeypatch.setattr(parser, "_http_get", fake_http_get)

    packages = asyncio.run(parser.fetch_packages(client=None))
    names = {p.name for p in packages}

    assert names == {"openssl", "musl"}
    openssl = next(p for p in packages if p.name == "openssl")
    assert openssl.version == "3.3.2-r0"
    assert openssl.filename == "openssl-3.3.2-r0.apk"
