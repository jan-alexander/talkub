from talkub import yamlio
from talkub.schematic import customization, customization_hash, schematic_document


def test_empty_customization():
    assert customization([], []) == {}
    assert schematic_document([], []) == "customization: {}\n"


def test_customization_sorts_extensions_and_keeps_kernel_args():
    assert customization(["siderolabs/tailscale", "siderolabs/intel-ucode"], ["console=ttyS0"]) == {
        "extraKernelArgs": ["console=ttyS0"],
        "systemExtensions": {"officialExtensions": ["siderolabs/intel-ucode", "siderolabs/tailscale"]},
    }


def test_document_is_the_factory_request_body():
    doc = yamlio.load(schematic_document(["siderolabs/tailscale"], []))
    assert doc == {"customization": {"systemExtensions": {"officialExtensions": ["siderolabs/tailscale"]}}}


def test_hash_ignores_extension_order_but_not_content():
    a = customization_hash(["b", "a"], [])
    assert a == customization_hash(["a", "b"], [])
    assert a != customization_hash(["a"], [])
    assert len(a) == 64
