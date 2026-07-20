"""watchdog — process parsing + in-run log rotation (copy-truncate)."""

import watchdog


def test_parse_running_and_missing():
    ps = ("/usr/bin/something else\n"
          "/Users/x/miniforge3/bin/python -u app.py\n"
          "/Users/x/miniforge3/bin/python -u strategy2_scanner.py\n")
    running = watchdog.parse_running(ps)
    assert running == {"app.py", "strategy2_scanner.py"}
    assert watchdog.missing(running) == ["bot.py", "strategy3_scanner.py"]


def test_rotate_logs_copy_truncates_only_oversized(tmp_path):
    big = tmp_path / "app.log"
    small = tmp_path / "bot.log"
    other = tmp_path / "notes.txt"
    big.write_text("x" * 500)
    small.write_text("y" * 10)
    other.write_text("z" * 500)

    rotated = watchdog.rotate_logs(str(tmp_path), max_bytes=100)

    assert rotated == ["app.log"]
    assert (tmp_path / "app.log.1").read_text() == "x" * 500   # history kept
    assert big.read_text() == ""                               # truncated in place
    assert small.read_text() == "y" * 10                       # untouched
    assert not (tmp_path / "notes.txt.1").exists()             # only *.log


def test_rotate_logs_missing_dir_is_noop(tmp_path):
    assert watchdog.rotate_logs(str(tmp_path / "nope")) == []
