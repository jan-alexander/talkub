import talkub.config as config

PUBLIC = (
    "MARKER ROLES PLATFORMS ARCHES DNS_LABEL Repo find_repo Settings NodeSpec ChartSpec "
    "SchematicSpec TailscaleSpec GitSpec Shortcut valid_label parse_installer_image"
).split()


def test_config_package_reexports_public_api():
    missing = [name for name in PUBLIC if not hasattr(config, name)]
    assert missing == []


def test_repo_module_only_locates_and_persists():
    from talkub.config import repo

    assert not hasattr(repo, "NodeSpec")
    assert not hasattr(repo, "Shortcut")
