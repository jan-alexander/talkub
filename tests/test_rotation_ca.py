import subprocess

from helpers import need

from talkub import rotation, yamlio

BUNDLE = (
    "cluster:\n    id: cid\n    secret: csecret\n"
    "secrets:\n    bootstraptoken: abc.def\n"
    "certs:\n    etcd:\n        crt: ETCDCRT\n        key: ETCDKEY\n"
    "    os:\n        crt: OLDCRT\n        key: OLDKEY\n"
)


def test_replace_os_ca_changes_only_certs_os():
    out = rotation.replace_os_ca(BUNDLE.encode(), "NEWCRT", "NEWKEY").decode()
    assert out == BUNDLE.replace("OLDCRT", "NEWCRT").replace("OLDKEY", "NEWKEY")
    doc = yamlio.load(out)
    assert doc["certs"]["etcd"]["key"] == "ETCDKEY" and doc["secrets"]["bootstraptoken"] == "abc.def"


@need("talosctl")
def test_replace_os_ca_against_real_bundle(tmp_path):
    bundle_path = tmp_path / "secrets.yaml"
    subprocess.run(["talosctl", "gen", "secrets", "-o", str(bundle_path)], check=True, capture_output=True)
    original = bundle_path.read_bytes()

    out = rotation.replace_os_ca(original, "NEWCRT", "NEWKEY")

    original_lines = original.decode().splitlines()
    out_lines = out.decode().splitlines()
    assert len(original_lines) == len(out_lines)
    changed = [i for i, (a, b) in enumerate(zip(original_lines, out_lines)) if a != b]
    assert len(changed) == 2
    assert out_lines[changed[0]].strip() == "crt: NEWCRT"
    assert out_lines[changed[1]].strip() == "key: NEWKEY"
