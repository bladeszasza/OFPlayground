"""Exercise real redirected CLI streams and process cleanup, without providers."""
import os
import subprocess
import sys


def test_redirected_demo_session_exits_without_encoding_error(tmp_path):
    env = {**os.environ, "OFP_STATUS_DISABLED": "1", "PYTHONIOENCODING": "cp1252"}
    result = subprocess.run([
        sys.executable, "-m", "ofp_playground", "start", "--no-human", "--policy", "round_robin",
        "--max-turns", "2", "--agent", "demo:Alpha:Synthetic worker",
        "--agent", "demo:Beta:Synthetic peer",
    ], cwd=tmp_path, env=env, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode("cp1252", errors="replace")
    assert b"completed step" in result.stdout
    assert b"UnicodeEncodeError" not in result.stderr
