"""HARNESS-SURVEY #11: symbol-level repo_map for non-Python source."""
from magic_core.magic.toolhost import ShellToolHost, ToolCall


def _syms(text, suffix):
    return ShellToolHost._source_symbols(text, suffix)


def test_kotlin_classes_and_funs():
    kt = (
        "package com.magic\n\n"
        "class SecurityActivity : AppCompatActivity() {\n"
        "    override fun onCreate(savedInstanceState: Bundle?) {}\n"
        "    private fun checkPhone() {}\n"
        "    suspend fun deepScan(pkg: String): Int { return 0 }\n"
        "}\n\n"
        "object AdbManager {\n"
        "    fun shell(cmd: String) {}\n"
        "}\n"
        "interface Guard { fun snapshot() }\n"
    )
    out = _syms(kt, ".kt")
    assert "class SecurityActivity" in out
    assert "object AdbManager" in out
    assert "interface Guard" in out
    assert "fun onCreate(" in out
    assert "fun checkPhone(" in out
    assert "fun deepScan(" in out


def test_java_class_and_method():
    java = (
        "public class Audit {\n"
        "    public static void main(String[] args) {}\n"
        "    private int score(String p) { return 1; }\n"
        "}\n"
    )
    out = _syms(java, ".java")
    assert "class Audit" in out
    assert "main(" in out
    assert "score(" in out


def test_js_functions_and_classes():
    js = (
        "export function fetchTop(url) {}\n"
        "class Renderer {}\n"
        "export const handle = async (req) => {};\n"
    )
    out = _syms(js, ".js")
    assert "function fetchTop" in out
    assert "class Renderer" in out
    assert "function handle" in out  # arrow-const is labelled a function


def test_go_and_rust():
    go = "func Serve(addr string) {}\ntype Server struct {\n}\n"
    out = _syms(go, ".go")
    assert "func Serve(" in out and "type Server" in out
    rs = "pub fn run() {}\nstruct Config {}\ntrait Guard {}\n"
    out = _syms(rs, ".rs")
    assert "fn run(" in out and "struct Config" in out and "trait Guard" in out


def test_unknown_language_is_empty():
    assert _syms("some plain text\nno code here\n", ".txt") == []


def test_repo_map_tool_shows_kotlin_symbols(tmp_path):
    (tmp_path / "SecurityActivity.kt").write_text(
        "class SecurityActivity {\n    fun checkPhone() {}\n}\n")
    (tmp_path / "notes.md").write_text("just notes\n")
    th = ShellToolHost(str(tmp_path))
    res = th.run(ToolCall(name="repo_map", args={"query": "phone"}))
    assert res.ok
    assert "SecurityActivity.kt" in res.output
    assert "class SecurityActivity" in res.output
    assert "fun checkPhone(" in res.output
