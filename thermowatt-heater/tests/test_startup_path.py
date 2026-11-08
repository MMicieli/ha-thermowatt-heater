"""The proactive-refresh switch through the REAL Supervisor startup path (issue #17).

Runs the unmodified add-on `run.sh` under the real bashio library, reading its options
from the Supervisor API stub (tests/e2e/supervisor_stub.py, the same stub the Docker e2e
harness uses), and checks the value the bridge process would see. Only the final
`python3 /thermowatt_bridge.py` exec is replaced by a probe that imports the real bridge
module and prints its effective setting, so no cloud or MQTT is needed.

Needs the bashio library (not pip-installable). Set TW_BASHIO_LIB to the directory that
holds bashio.sh, e.g. after:
    curl -sL https://github.com/hassio-addons/bashio/archive/refs/tags/v0.16.2.tar.gz | tar xz
    export TW_BASHIO_LIB=$PWD/bashio-0.16.2/lib
Without it these tests are SKIPPED (reported as skipped, never as passed).
"""

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.dirname(HERE)
STUB = os.path.join(HERE, "e2e", "supervisor_stub.py")
BASHIO_LIB = os.environ.get("TW_BASHIO_LIB", "")

pytestmark = pytest.mark.skipif(
    not (BASHIO_LIB and os.path.exists(os.path.join(BASHIO_LIB, "bashio.sh"))
         and os.path.exists("/bin/bash")),
    reason="TW_BASHIO_LIB (bashio library directory) not set")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_startup(tmp_path, options):
    port = _free_port()
    token = "stub-supervisor-token"
    env = dict(os.environ, SUPERVISOR_TOKEN=token, STUB_PORT=str(port),
               ADDON_OPTIONS_JSON=json.dumps(options),
               MQTT_SERVICE_JSON=json.dumps({"host": "mq", "port": 1883,
                                             "username": "u", "password": "p"}))
    stub = subprocess.Popen([sys.executable, "-I", STUB], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2)
            except urllib.error.HTTPError:
                break
            except OSError:
                time.sleep(0.1)
        shim_dir = tmp_path / "shim"
        shim_dir.mkdir()
        probe = shim_dir / "probe.py"
        probe.write_text(textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {ADDON!r})
            sys.argv = ["thermowatt_bridge.py"] + sys.argv[2:]
            import thermowatt_bridge as m
            print("ENV=" + os.environ.get("THERMOWATT_PROACTIVE_REFRESH", "<unset>"))
            print("EFFECTIVE=" + str(m.MyThermowattBridge.PROACTIVE_REFRESH))
        """))
        # A shell function shadows `python3` inside run.sh (no executable shim needed).
        script = (f"python3() {{ {sys.executable} {probe} \"$@\"; }}; "
                  f"source {BASHIO_LIB}/bashio.sh; source {os.path.join(ADDON, 'run.sh')}")
        env2 = dict(env, SUPERVISOR_API=f"http://127.0.0.1:{port}",
                    CACHE_DIR=str(tmp_path / "bashio-cache"))
        out = subprocess.run(["/bin/bash", "-c", script], env=env2, capture_output=True,
                             text=True, timeout=60)
        assert out.returncode == 0, out.stdout + out.stderr
        lines = dict(l.split("=", 1) for l in out.stdout.splitlines() if l[:4] in ("ENV=", "EFFE"))
        return lines["ENV"], lines["EFFECTIVE"]
    finally:
        stub.terminate()
        stub.wait(timeout=5)


BASE = {"email": "stub@example.test", "password": "x", "element_wattage": 3000}


@pytest.mark.parametrize("options,env_value,effective", [
    (dict(BASE, proactive_token_refresh=True), "1", "True"),
    (dict(BASE, proactive_token_refresh=False), "0", "False"),
    (dict(BASE), "0", "False"),                                    # option absent (upgrade)
    (dict(BASE, proactive_token_refresh="yes"), "0", "False"),     # malformed, not a bool
    (dict(BASE, proactive_token_refresh=None), "0", "False"),
])
def test_option_reaches_the_bridge_process(tmp_path, options, env_value, effective):
    assert _run_startup(tmp_path, options) == (env_value, effective)


def test_inherited_container_env_cannot_override_a_disabled_option(tmp_path, monkeypatch):
    # run.sh always exports the option-derived value, so a stray inherited variable
    # cannot switch the feature on when the add-on option is false.
    monkeypatch.setenv("THERMOWATT_PROACTIVE_REFRESH", "1")
    assert _run_startup(tmp_path, dict(BASE, proactive_token_refresh=False)) == ("0", "False")
