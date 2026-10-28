

def test_claim_worker_command_line_fits_windows(monkeypatch, tmp_path):
    """CreateProcess refuses command lines over 32,767 characters, so the worker's claim cannot ride in argv."""
    launched = []

    def fake_run(argv, **kwargs):
        launched.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"execution": 0, "imports": 0}', "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    test_new_file_claim_shapes_remain_bounded("a/" * 30000 + "z", tmp_path)
    assert len(subprocess.list2cmdline(launched[0])) < 32767
