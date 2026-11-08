"""Generic local-plugin hook: modules in local_plugins/ can add routes."""
import asyncio as aio
import threading

from gremlin_core.server import create_app


class Reg:
    def names(self):
        return []

    def primary_model_name(self):
        return None


def test_local_plugins_add_routes_and_broken_ones_are_skipped(tmp_path):
    d = tmp_path / "local_plugins"
    d.mkdir()
    (d / "hello.py").write_text(
        "def register(app, ctx):\n"
        "    @app.route('/local-hello')\n"
        "    def hello():\n"
        "        err = ctx['check_auth']()\n"
        "        return err if err else 'hi'\n")
    (d / "broken.py").write_text("raise RuntimeError('nope')\n")
    (d / "test_ignored.py").write_text("raise SystemExit('should not be imported')\n")
    lp = aio.new_event_loop()
    th = threading.Thread(target=lp.run_forever, daemon=True)
    th.start()
    try:
        c = create_app(Reg(), router=None, project_root=tmp_path, loop=lp,
                       token="t", admin_token="admin").test_client()
        assert c.get("/local-hello").status_code == 401
        assert c.get("/local-hello", headers={"Authorization": "Bearer t"}).data == b"hi"
    finally:
        lp.call_soon_threadsafe(lp.stop)
        th.join(timeout=5)


def test_no_local_plugins_folder_is_fine(tmp_path):
    lp = aio.new_event_loop()
    try:
        create_app(Reg(), router=None, project_root=tmp_path, loop=lp, token="t", admin_token="a")
    finally:
        lp.close()
