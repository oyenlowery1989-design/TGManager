#!/bin/bash
# Run on a dedicated, sufficiently sized Linux x86_64 builder, never the live VPS.
set -euo pipefail
umask 077
source_commit=d4b7546e77f93ac4c9bb53a3fcf7e9ffbbecf1b5
here=$(cd -- "$(dirname -- "$0")" && pwd -P)
patch="$here/ayugram-inline.patch"
work=${1:?Usage: build_linux.sh NEW_EMPTY_WORKSPACE}
[[ $(uname -sm) == 'Linux x86_64' ]] || { echo 'Linux x86_64 is required' >&2; exit 1; }
[[ "$work" == /* && "$work" != *[[:space:]]* && ! -e "$work" ]] || { echo 'Use a new absolute workspace without spaces' >&2; exit 1; }
[[ -f "$patch" && ! -L "$patch" ]] || { echo 'Prepare the isolated build context first' >&2; exit 1; }
: "${TDESKTOP_API_ID:?Supply the Telegram application API ID through the build environment}"
: "${TDESKTOP_API_HASH:?Supply the Telegram application API hash through the build environment}"
[[ "$TDESKTOP_API_ID" =~ ^[0-9]+$ && "$TDESKTOP_API_HASH" =~ ^[0-9a-f]{32}$ ]] || exit 1
for program in git docker python3 sha256sum tar; do command -v "$program" >/dev/null; done
python3 -c 'import jinja2'
docker buildx version >/dev/null
# This is a conservative preflight, not a guarantee that the build will fit.
(( $(awk '/MemAvailable:/ {print $2}' /proc/meminfo) >= 24000000 )) || { echo 'Builder needs at least 24 GB available RAM' >&2; exit 1; }
mkdir -m 700 -- "$work"
(( $(df -Pk "$work" | awk 'NR==2 {print $4}') >= 125000000 )) || { echo 'Builder needs at least 125 GB free disk' >&2; exit 1; }
echo 'Building pinned custom AyuGram; detailed output stays in the private workspace.'
# Upstream CMake can echo its API arguments. Never stream that log to Cloud Build.
exec >"$work/build.log" 2>&1
cd "$work"
git init source
git -C source remote add origin https://github.com/AyuGram/AyuGramDesktop.git
git -C source fetch --depth=1 origin "$source_commit"
git -C source checkout --detach FETCH_HEAD
test "$(git -C source rev-parse HEAD)" = "$source_commit"
git -C source apply --check "$patch"
git -C source apply "$patch"
git -C source submodule update --init --recursive --depth=1
cat >buildkit.toml <<'EOF'
[worker.oci]
  max-parallelism = 1
EOF
builder="ayu-$(date +%s)-$$"
cleanup() { docker buildx rm "$builder" >/dev/null 2>&1 || true; }
trap cleanup EXIT
# Limit nproc-driven dependency jobs as well as independent BuildKit stages.
docker buildx create --name "$builder" --driver docker-container \
  --driver-opt cpuset-cpus=0-7 --driver-opt memory=24g --driver-opt memory-swap=24g \
  --buildkitd-config "$work/buildkit.toml"
cd source/Telegram/build/docker/centos_env
DEBUG= LTO= JOBS=8 python3 gen_dockerfile.py > "$work/dependencies.Dockerfile"
# Some Rocky mirrors return stale metadata; keep GPG checks and use its primary HTTPS repository.
python3 - "$work/dependencies.Dockerfile" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
source = path.read_text()
needle = 'dnf -y install epel-release\n'
if source.count(needle) != 1:
    raise ValueError('Pinned dependency recipe changed; review repository setup')
setup = r'''for mapping in BaseOS:BaseOS AppStream:AppStream PowerTools:PowerTools Extras:extras; do
    repo_file="/etc/yum.repos.d/Rocky-${mapping%%:*}.repo"
    repo_path="${mapping#*:}"
    test -f "$repo_file"
    sed -i -e '/^mirrorlist=/s/^/#/' \
        -e "s|^#baseurl=.*|baseurl=https://dl.rockylinux.org/pub/rocky/8/$repo_path/\$basearch/os/|" \
        -e "s|^baseurl=.*|baseurl=https://dl.rockylinux.org/pub/rocky/8/$repo_path/\$basearch/os/|" "$repo_file"
done
dnf clean metadata
'''
path.write_text(source.replace(needle, setup + needle))
PY
docker buildx build --builder "$builder" --load -t ayugram-pinned:linux \
  -f "$work/dependencies.Dockerfile" .
cd "$work/source"
docker run --rm --cpuset-cpus=0-7 --memory=24g --memory-swap=24g \
  -u "$(id -u)" -v "$work/source:/usr/src/tdesktop" -w /usr/src/tdesktop \
  -e CONFIG=Release -e CMAKE_BUILD_PARALLEL_LEVEL=8 -e CARGO_BUILD_JOBS=8 \
  -e TDESKTOP_API_ID -e TDESKTOP_API_HASH ayugram-pinned:linux \
  /bin/bash -c 'exec /usr/src/tdesktop/Telegram/build/docker/centos_env/build.sh \
    -D CMAKE_CONFIGURATION_TYPES=Release -D DESKTOP_APP_ENABLE_LTO=OFF \
    -D TDESKTOP_API_ID="$TDESKTOP_API_ID" -D TDESKTOP_API_HASH="$TDESKTOP_API_HASH"'
mkdir "$work/release"
cp -- 'out/Release/AyuGram Inline Test' "$work/release/"
cp -- "$patch" "$work/release/ayugram-inline.patch"
cp -- LICENSE "$work/release/UPSTREAM-LICENSE"
python3 - "$work/release" "$source_commit" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            result.update(block)
    return result.hexdigest()
name = 'AyuGram Inline Test'
(root/'release.json').write_text(json.dumps(dict(schema=1, platform='linux-x86_64',
    binary_name=name, source_commit=sys.argv[2], binary_sha256=sha(root/name),
    patch_sha256=sha(root/'ayugram-inline.patch')), indent=2)+'\n')
(root/'SOURCE.txt').write_text('https://github.com/AyuGram/AyuGramDesktop/tree/'+sys.argv[2]+
    '\nApply the accompanying ayugram-inline.patch to this exact commit.\n')
PY
tar -czf "$work/ayugram-linux-x86_64.tar.gz" -C "$work/release" .
sha256sum "$work/ayugram-linux-x86_64.tar.gz" > "$work/ayugram-linux-x86_64.tar.gz.sha256"
