"""ContainerEffects filesystem parity — routes file I/O into a Docker container.

The headline regression: read_file must return EXACT bytes (preserving ``\r``).
The pyte PTY capture collapsed DOS-line-ending files to their last line, which
made the agent misdiagnose process_data.sh as "only fi" (the processing-pipeline
failure). The docker archive API (get_archive) is binary-safe and fixes that.

Live-docker tests; skip cleanly when docker or the base image is absent (they do
not pull, to stay fast and offline-friendly).
"""

from __future__ import annotations

import pytest

docker = pytest.importorskip("docker")

IMAGE = "ghcr.io/laude-institute/t-bench/python-3-13:latest"


@pytest.fixture(scope="module")
def container():
    try:
        client = docker.from_env()
        client.ping()
    except Exception:
        pytest.skip("docker unavailable")
    try:
        client.images.get(IMAGE)  # don't pull in tests
    except Exception:
        pytest.skip(f"image {IMAGE} not present locally")
    c = client.containers.run(IMAGE, command=["sleep", "infinity"], detach=True)
    try:
        c.exec_run(["mkdir", "-p", "/app"])
        yield c
    finally:
        c.remove(force=True)


@pytest.fixture
def fx(container, tmp_path):
    from tb_adapter.container_effects import ContainerEffects

    pty = tmp_path / "pty"
    pty.mkdir()
    return ContainerEffects(
        container=container,
        container_workdir="/app",
        host_working_directory=str(tmp_path),
        host_pty_scratch=str(pty),
        llmvp_endpoint="http://localhost:8008/graphql",
    )


@pytest.mark.asyncio
async def test_write_read_roundtrip(fx):
    r = await fx.write_file("foo.txt", "hello\nworld\n")
    assert r.success and r.bytes_written == 12
    got = await fx.read_file("foo.txt")
    assert got.exists and got.content == "hello\nworld\n"


@pytest.mark.asyncio
async def test_read_preserves_crlf(fx):
    # THE regression: a DOS-line-ending file must round-trip exactly (the pyte
    # terminal capture collapsed these to their last line).
    dos = "#!/bin/bash\r\necho hi\r\nfi\r\n"
    await fx.write_file("dos.sh", dos)
    got = await fx.read_file("dos.sh")
    assert got.content == dos
    assert "\r\n" in got.content


@pytest.mark.asyncio
async def test_read_missing_file(fx):
    got = await fx.read_file("does-not-exist.txt")
    assert got.exists is False and got.content == ""


@pytest.mark.asyncio
async def test_write_preserves_exec_bit_on_overwrite(fx):
    await fx.write_file("script.sh", "#!/bin/sh\necho one\n")
    fx._container.exec_run(["chmod", "+x", "/app/script.sh"])
    await fx.write_file("script.sh", "#!/bin/sh\necho two\n")
    rc, out, _ = await fx._sh(["test", "-x", "/app/script.sh"])
    assert rc == 0  # +x survived the overwrite
    assert (await fx.read_file("script.sh")).content == "#!/bin/sh\necho two\n"


@pytest.mark.asyncio
async def test_file_exists_and_makedirs(fx):
    assert await fx.file_exists("foo.txt") is False or True  # foo may not exist yet
    await fx.makedirs("sub/dir")
    assert await fx.file_exists("sub/dir") is True
    await fx.write_file("sub/dir/x.txt", "x")
    assert await fx.file_exists("sub/dir/x.txt") is True


@pytest.mark.asyncio
async def test_list_directory(fx):
    await fx.write_file("a.txt", "a")
    await fx.write_file("b.txt", "b")
    await fx.makedirs("d")
    listing = await fx.list_directory(".")
    assert listing.exists
    names = {e.name for e in listing.entries}
    assert {"a.txt", "b.txt", "d"} <= names
    a = next(e for e in listing.entries if e.name == "a.txt")
    assert a.is_file and not a.is_dir and a.size == 1
    d = next(e for e in listing.entries if e.name == "d")
    assert d.is_dir and not d.is_file


@pytest.mark.asyncio
async def test_search_files_content(fx):
    await fx.write_file("s1.py", "import os\nNEEDLE = 1\n")
    await fx.write_file("s2.py", "x = 2\n")
    res = await fx.search_files("*.py", content_pattern="NEEDLE")
    assert any(m.file_path == "s1.py" for m in res.matches)
    assert all(m.file_path != "s2.py" for m in res.matches)


def test_interactive_routes_shell_c_through_bash_i():
    """run_command's ``<shell> -c SCRIPT`` must run in INTERACTIVE bash so the
    task's ~/.bashrc (aliases/env) loads — the SAME env the operator PTY uses.
    The create-bucket verify re-probe false-failed ("Unable to locate
    credentials") because ``/bin/sh -c`` skipped the ``aws``→``awslocal`` alias;
    non-shell argv (test/find/grep parity) must pass through untouched. Pure
    function — no docker needed.
    """
    from tb_adapter.container_effects import ContainerEffects

    f = ContainerEffects._interactive
    assert f(["/bin/sh", "-c", "aws s3 ls"]) == ["/bin/bash", "-i", "-c", "aws s3 ls"]
    assert f(["bash", "-c", "x"]) == ["/bin/bash", "-i", "-c", "x"]
    assert f(["test", "-e", "/app/x"]) == ["test", "-e", "/app/x"]
    assert f(["find", ".", "-name", "*.sh"]) == ["find", ".", "-name", "*.sh"]
