"""`!cmd` shell lines in code cells (kernel backend). Base install: no numpy needed."""
import os
import sys
import time

import pytest

from server import kernel_server as ks


@pytest.fixture
def dialog():
    name = f"shell-{time.monotonic_ns()}"
    yield name


def run(d, code):
    return ks.run_code(d, code)[0]


def test_bang_echo(dialog):
    assert run(dialog, "!echo hi") == "hi"


def test_mixed_cell_order(dialog):
    assert run(dialog, "x = 1\n!echo mid\nprint(x)") == "mid\n1"


def test_bang_in_indented_body(dialog):
    assert run(dialog, "for i in range(2):\n    !echo row\n") == "row\nrow"


def test_valid_python_with_bang_equals_untouched(dialog):
    assert run(dialog, "x = (1\n!= 2)\nprint(x)") == "True"


def test_exit_code(dialog):
    out = run(dialog, "!false\nprint(_exit_code)")
    assert "[exit 1]" in out and out.endswith("1")


def test_plain_syntax_error_reported(dialog):
    assert run(dialog, "def (:").startswith("SyntaxError")


def test_env_uses_kernel_python(dialog):
    assert run(dialog, '!python -c "import sys; print(sys.prefix)"') == sys.prefix


def test_capture(dialog):
    out = run(dialog, "files = !printf 'a\\nb\\n'\nprint(files)")
    assert out == "['a', 'b']"


def test_stream(dialog):
    rid = ks.exec_start(dialog, "!echo a; sleep 2; echo b")
    deadline = time.time() + 5
    while time.time() < deadline:
        p = ks.exec_poll(dialog, rid)
        if "a" in p["output"]:
            break
        time.sleep(0.05)
    assert "a" in p["output"] and not p["done"]
    while not ks.exec_poll(dialog, rid)["done"]:
        time.sleep(0.05)


def _stop_and_check(dialog, cmd):
    rid = ks.exec_start(dialog, cmd)
    pid = None
    deadline = time.time() + 5
    while time.time() < deadline and pid is None:
        out = ks.exec_poll(dialog, rid)["output"].strip()
        if out.isdigit():
            pid = int(out)
        time.sleep(0.05)
    assert pid
    t0 = time.time()
    ks.exec_stop(dialog, rid)
    while not ks.exec_poll(dialog, rid)["done"] and time.time() - t0 < 5:
        time.sleep(0.05)
    assert ks.exec_poll(dialog, rid)["done"]
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_stop(dialog):
    _stop_and_check(dialog, "!sh -c 'echo $$; exec sleep 30'")


def test_stop_kills_sigterm_ignoring_child(dialog):
    _stop_and_check(dialog, "!sh -c 'trap \"\" TERM; echo $$; while :; do sleep 1; done'")


def test_background_job_does_not_hang_cell(dialog):
    t0 = time.time()
    run(dialog, "!sleep 4 &\nprint('done')")
    assert time.time() - t0 < 3


def test_error_after_bang_reports_real_line(dialog):
    assert "line 2" in run(dialog, "!ls\ndef (:")


def test_crlf_bang(dialog):
    assert run(dialog, "!echo hi\r\nprint(1)") == "hi\n1"


def test_render_code_highlights_bang():
    from dandelion import app
    assert 'class="err"' not in str(app.render_code("!ls -la\nx = 1\n"))


def test_highlight_bang_line():
    from dandelion import app
    html = app._highlight("!ls -la\nx = 1\n")
    assert 'class="err"' not in html
    assert "x" in html and 'class="mi"' in html
