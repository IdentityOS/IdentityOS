#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Install IdentityOS into an isolated user-owned environment.

Usage:
  curl -fsSL https://raw.githubusercontent.com/IdentityOS/IdentityOS/main/install.sh | bash
  IDENTITYOS_REF=<tag-or-commit> ./install.sh
  ./install.sh --artifact /path/to/identityos-X.Y.Z.whl
  ./install.sh --artifact /path/to/source-tree
  ./install.sh --version X.Y.Z

Options:
  --artifact PATH   Local wheel, source archive, or source directory.
  --ref REF         Git tag, branch, or commit archive (default: main).
  --version VERSION Exact version from the configured Python package index.
  --python PATH     Python 3.11+ interpreter (default: python3).
  --install-root P  Versioned environments (default: user data directory).
  --bin-dir PATH    Command link directory (default: ~/.local/bin).
  --no-deps         Do not resolve dependencies (offline/test artifacts only).
  -h, --help        Show this help.

This installer never clones a repository, edits .env, downloads a model, starts
a service, opens a public port, or requires administrator access.
EOF
}

artifact=""
version=""
ref=""
python_bin="${IDENTITYOS_PYTHON:-python3}"
install_root="${IDENTITYOS_INSTALL_ROOT:-${HOME}/.local/share/identityos-runtime}"
bin_dir="${IDENTITYOS_BIN_DIR:-${HOME}/.local/bin}"
no_deps=0

while (($#)); do
  case "$1" in
    --artifact) artifact="${2:-}"; shift 2 ;;
    --ref) ref="${2:-}"; shift 2 ;;
    --version) version="${2:-}"; shift 2 ;;
    --python) python_bin="${2:-}"; shift 2 ;;
    --install-root) install_root="${2:-}"; shift 2 ;;
    --bin-dir) bin_dir="${2:-}"; shift 2 ;;
    --no-deps) no_deps=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$artifact" && -z "$version" && -z "$ref" ]]; then
  ref="${IDENTITYOS_REF:-main}"
fi
source_count=0
[[ -n "$artifact" ]] && source_count=$((source_count + 1))
[[ -n "$version" ]] && source_count=$((source_count + 1))
[[ -n "$ref" ]] && source_count=$((source_count + 1))
if ((source_count != 1)); then
  printf 'Choose exactly one of --artifact, --ref, or --version.\n' >&2
  exit 2
fi
if [[ -n "$version" && ! "$version" =~ ^[0-9]+(\.[0-9]+){1,3}([A-Za-z0-9._+-]*)?$ ]]; then
  printf 'Version must be exact, for example 2.1.0.\n' >&2
  exit 2
fi
if [[ -n "$ref" ]]; then
  if [[ ! "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$ || "$ref" == *".."* ]]; then
    printf 'Ref contains unsupported characters.\n' >&2
    exit 2
  fi
  if ! command -v curl >/dev/null 2>&1; then
    printf 'curl is required to install from a Git ref.\n' >&2
    exit 2
  fi
fi
if [[ -n "$artifact" ]]; then
  artifact="$(cd "$(dirname "$artifact")" && pwd -P)/$(basename "$artifact")"
  if [[ ! -e "$artifact" ]]; then
    printf 'Artifact does not exist: %s\n' "$artifact" >&2
    exit 2
  fi
fi
if ! command -v "$python_bin" >/dev/null 2>&1; then
  printf 'Python was not found: %s\n' "$python_bin" >&2
  exit 2
fi
if ! "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  printf 'IdentityOS requires Python 3.11 or newer.\n' >&2
  exit 2
fi

mkdir -p "$install_root/releases" "$bin_dir"
release_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
release_dir="$install_root/releases/$release_id"
cleanup() {
  if [[ -n "${release_dir:-}" && -d "$release_dir" && ! -f "$release_dir/.complete" ]]; then
    rm -rf -- "$release_dir"
  fi
  if [[ -n "${download_dir:-}" && -d "$download_dir" ]]; then
    rm -rf -- "$download_dir"
  fi
}
trap cleanup EXIT INT TERM

download_dir=""
if [[ -n "$ref" ]]; then
  download_dir="$(mktemp -d)"
  encoded_ref="${ref//\//%2F}"
  repository_url="${IDENTITYOS_REPOSITORY_URL:-https://github.com/IdentityOS/IdentityOS}"
  artifact="$download_dir/identityos-source.tar.gz"
  curl -fsSL --retry 2 --proto '=https' --tlsv1.2 \
    "$repository_url/archive/$encoded_ref.tar.gz" -o "$artifact"
  if command -v sha256sum >/dev/null 2>&1; then
    artifact_sha256="$(sha256sum "$artifact" | awk '{print $1}')"
  else
    artifact_sha256="$(shasum -a 256 "$artifact" | awk '{print $1}')"
  fi
  printf 'Downloaded ref %s (SHA-256 %s)\n' "$ref" "$artifact_sha256"
fi

# The virtualenv is created at its permanent versioned path because venv script
# shebangs are not relocatable. The active link is untouched until verification.
"$python_bin" -m venv "$release_dir"
pip_args=(install --disable-pip-version-check)
if ((no_deps)); then pip_args+=(--no-deps); fi
if [[ -n "$artifact" ]]; then
  "$release_dir/bin/python" -m pip "${pip_args[@]}" "$artifact"
else
  "$release_dir/bin/python" -m pip "${pip_args[@]}" "identityos==$version"
fi

if [[ ! -x "$release_dir/bin/idos" ]]; then
  printf 'Installed artifact does not provide the required idos command.\n' >&2
  exit 1
fi
"$release_dir/bin/python" -c 'import cli.idos; raise SystemExit(0)'
if [[ -f "$0" && "$(basename "$0")" != "bash" && "$(basename "$0")" != "sh" ]]; then
  cp "$0" "$install_root/install.sh"
elif [[ -n "$ref" ]]; then
  raw_repository_url="${IDENTITYOS_RAW_REPOSITORY_URL:-https://raw.githubusercontent.com/IdentityOS/IdentityOS}"
  curl -fsSL --proto '=https' --tlsv1.2 "$raw_repository_url/$ref/install.sh" -o "$install_root/install.sh"
fi
if [[ -f "$install_root/install.sh" ]]; then
  chmod 0755 "$install_root/install.sh"
fi
printf 'managed IdentityOS installation\n' > "$install_root/.identityos-install"

# Only the final link swap changes the active application. Preparation failures
# above leave the prior release and command links untouched.
touch "$release_dir/.complete"
tmp_link="$install_root/.current-$release_id"
ln -s "$release_dir" "$tmp_link"
"$python_bin" -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$tmp_link" "$install_root/current"
ln -sfn "$install_root/current/bin/idos" "$bin_dir/idos"
if [[ -x "$install_root/current/bin/identity" ]]; then
  ln -sfn "$install_root/current/bin/identity" "$bin_dir/identity"
fi
if [[ -n "$download_dir" ]]; then
  rm -rf -- "$download_dir"
fi
trap - EXIT INT TERM

printf 'IdentityOS installed in %s\n' "$release_dir"
printf 'Command: %s/idos\n' "$bin_dir"
if [[ ":${PATH}:" != *":${bin_dir}:"* ]]; then
  printf 'Add %s to PATH, then run: idos init\n' "$bin_dir"
else
  printf 'Next: idos init\n'
fi
