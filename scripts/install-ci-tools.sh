#!/bin/sh
# Install the external tools the test suite needs (see helpers.need) into $1 (default ~/.local/bin).
# Linux amd64 only (CI). Versions are pinned here; bump them together with your local toolchain.
set -eu

AGE_VERSION=1.3.2
SOPS_VERSION=3.13.3
TALOSCTL_VERSION=1.14.0
HELM_VERSION=4.3.0
KUBECONFORM_VERSION=0.8.0
KUBESEAL_VERSION=0.40.0

bin="${1:-$HOME/.local/bin}"
mkdir -p "$bin"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fetch() { curl -fsSL --retry 3 -o "$2" "$1"; }

fetch "https://github.com/FiloSottile/age/releases/download/v${AGE_VERSION}/age-v${AGE_VERSION}-linux-amd64.tar.gz" "$tmp/age.tgz"
tar -xzf "$tmp/age.tgz" -C "$tmp"
install -m 755 "$tmp/age/age" "$tmp/age/age-keygen" "$bin/"

fetch "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" "$tmp/sops"
install -m 755 "$tmp/sops" "$bin/"

fetch "https://github.com/siderolabs/talos/releases/download/v${TALOSCTL_VERSION}/talosctl-linux-amd64" "$tmp/talosctl"
install -m 755 "$tmp/talosctl" "$bin/"

fetch "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" "$tmp/helm.tgz"
tar -xzf "$tmp/helm.tgz" -C "$tmp"
install -m 755 "$tmp/linux-amd64/helm" "$bin/"

fetch "https://github.com/yannh/kubeconform/releases/download/v${KUBECONFORM_VERSION}/kubeconform-linux-amd64.tar.gz" "$tmp/kubeconform.tgz"
tar -xzf "$tmp/kubeconform.tgz" -C "$tmp" kubeconform
install -m 755 "$tmp/kubeconform" "$bin/"

fetch "https://github.com/bitnami-labs/sealed-secrets/releases/download/v${KUBESEAL_VERSION}/kubeseal-${KUBESEAL_VERSION}-linux-amd64.tar.gz" "$tmp/kubeseal.tgz"
tar -xzf "$tmp/kubeseal.tgz" -C "$tmp" kubeseal
install -m 755 "$tmp/kubeseal" "$bin/"
