"""Ask AI prompt history (Up/Down recall): pure module run under node + wiring."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dandelion import app

JS = Path(app.__file__).parent / "static" / "js" / "ask_history.js"

HARNESS = r"""
var window = {};
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
function fake(init){
  var d = {}; if(init !== undefined) d['dandelion.askHistory'] = init;
  return {d: d, getItem: function(k){ return k in d ? d[k] : null; },
          setItem: function(k, v){ d[k] = v; }};
}
var out = {};
// push: blanks and consecutive duplicates skipped, cap 200
var s = fake(), h = window.AskHistory(s);
['a', '  ', '', 'a', 'b', 'a'].forEach(function(t){ h.push(t); });
out.pushed = JSON.parse(s.d['dandelion.askHistory']);
s = fake(); h = window.AskHistory(s);
for(var i = 0; i < 205; i++) h.push('p' + i);
var all = JSON.parse(s.d['dandelion.askHistory']);
out.cap = [all.length, all[0], all[all.length - 1]];
// order and draft
s = fake(JSON.stringify(['one', 'two', 'three'])); h = window.AskHistory(s);
out.order = [h.older('draft'), h.older('x'), h.older('x'), h.older('x'),
             h.newer('x'), h.newer('x'), h.newer('x'), h.newer('x')];
out.empty = window.AskHistory(fake()).older('d');
h.older('d2'); h.reset(); out.afterReset = h.newer('z');
// throwing storage
var bad = {getItem: function(){ throw new Error('no'); }, setItem: function(){ throw new Error('no'); }};
h = window.AskHistory(bad); h.push('q'); h.push('r');
out.throwing = [h.older('d'), h.older('d'), h.newer(''), h.newer('')];
out.nostorage = (function(){ var g = window.AskHistory(undefined); g.push('k'); return g.older('d'); })();
out.garbage = window.AskHistory(fake('not json')).older('d');
console.log(JSON.stringify(out));
"""


def test_ask_history_module_under_node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    r = subprocess.run([node, "-e", HARNESS, str(JS)], check=False,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["pushed"] == ["a", "b", "a"]
    assert o["cap"] == [200, "p5", "p204"]
    assert o["order"] == ["three", "two", "one", None, "two", "three", "draft", None]
    assert o["empty"] is None
    assert o["afterReset"] is None
    assert o["throwing"] == ["r", "q", "r", "d"]
    assert o["nostorage"] == "k"
    assert o["garbage"] is None


def test_composer_wires_arrow_keys_and_module_loads_first():
    js = app.COMPOSER_JS
    assert "ArrowUp" in js and "ArrowDown" in js and "askHistory" in js
    src = Path(app.__file__).read_text(encoding="utf-8")
    assert src.index('src="/static/js/ask_history.js"') < src.index('src="/static/js/composer.js"')
