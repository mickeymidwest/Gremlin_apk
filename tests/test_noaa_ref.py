"""NOOA pass-by-reference: big tool outputs become a preview + ref handle,
read on demand with read_ref -- so a weak model's context isn't flooded."""
from gremlin_core.magic.toolhost import ShellToolHost, ToolCall


def _host(tmp_path):
    return ShellToolHost(str(tmp_path))


def test_small_output_passes_through(tmp_path):
    (tmp_path / "small.txt").write_text("hello\nworld\n")
    r = _host(tmp_path).run(ToolCall(name="read_file", args={"path": "small.txt"}))
    assert r.ok and "hello" in r.output and "stored as ref" not in r.output


def test_large_output_becomes_a_ref(tmp_path):
    big = "\n".join(f"line {i} with some content here" for i in range(400))
    (tmp_path / "big.txt").write_text(big)
    h = _host(tmp_path)
    r = h.run(ToolCall(name="read_file", args={"path": "big.txt"}))
    assert r.ok
    assert "stored as ref 1" in r.output
    assert "line 0" in r.output            # preview keeps the head
    assert "line 399" not in r.output      # the tail is NOT dumped
    assert len(r.output) < 1500            # context stays small
    assert len(h._refs) == 1 and len(h._refs[0]) == len(big)


def test_read_ref_pages_the_full_output(tmp_path):
    big = "\n".join(f"line {i}" for i in range(400))
    (tmp_path / "big.txt").write_text(big)
    h = _host(tmp_path)
    h.run(ToolCall(name="read_file", args={"path": "big.txt"}))
    # page from line 390 -> we can reach the tail the preview hid
    r = h.run(ToolCall(name="read_ref", args={"ref": 1, "start": 390, "count": 20}))
    assert r.ok and "line 399" in r.output


def test_read_ref_bad_handle(tmp_path):
    h = _host(tmp_path)
    r = h.run(ToolCall(name="read_ref", args={"ref": 9}))
    assert r.ok is False


def test_read_ref_is_explorable(tmp_path):
    # read_ref is a read, so it's in the explore phase toolset
    h = ShellToolHost(str(tmp_path), allowed=ShellToolHost.EXPLORE_TOOLS)
    assert "read_ref" in h.allowed
