import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare_remote_build', ROOT/'tools/prepare_remote_ayugram_build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class RemoteBuildTests(unittest.TestCase):
    def test_context_only_contains_explicit_public_build_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)/'context'
            receipt = build.prepare(destination)
            self.assertEqual(set(p.name for p in destination.iterdir()),
                             {'build_linux.sh','ayugram-inline.patch','context.json'})
            for name, digest in receipt.items():
                self.assertEqual(hashlib.sha256((destination/name).read_bytes()).hexdigest(),digest)
            with self.assertRaises(ValueError):
                build.prepare(destination)
            self.assertEqual(json.loads((destination/'context.json').read_text()),receipt)

    def test_build_pipeline_packages_matching_manifest_without_upload_or_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            context = root/'context'; build.prepare(context)
            commands = root/'bin'; commands.mkdir()
            def stub(name, body):
                path = commands/name
                path.write_text('#!/bin/bash\nset -eu\n'+body+'\n'); path.chmod(0o700)
            stub('uname', 'echo "Linux x86_64"')
            stub('awk', "echo 200000000")
            stub('python3', 'if [[ "${1:-}" == "-c" ]]; then exit 0; fi\nexec "'+os.sys.executable+'" "$@"')
            stub('git', '''
if [[ "$1" == init ]]; then
  mkdir -p source/Telegram/build/docker/centos_env
  echo 'print("FROM rockylinux:8\\nRUN <<EOF\\ndnf -y install epel-release\\nEOF")' > source/Telegram/build/docker/centos_env/gen_dockerfile.py
  echo 'synthetic license' > source/LICENSE
elif [[ "$3" == rev-parse ]]; then
  echo d4b7546e77f93ac4c9bb53a3fcf7e9ffbbecf1b5
fi
''')
            stub('docker', '''
printf '%s\\n' "$*" >> "$TEST_DOCKER_TRACE"
if [[ "$1" == run ]]; then
  mkdir -p out/Release
  echo 'synthetic binary, never executed' > 'out/Release/AyuGram Inline Test'
fi
''')
            trace = root/'docker-trace'
            work = root/'work'
            env = {**os.environ, 'PATH':str(commands)+os.pathsep+os.environ['PATH'],
                   'TDESKTOP_API_ID':'12345','TDESKTOP_API_HASH':'a'*32,
                   'TEST_DOCKER_TRACE':str(trace)}
            result = subprocess.run(['bash',str(context/'build_linux.sh'),str(work)],
                                    env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,(work/'build.log').read_text() if (work/'build.log').exists() else result.stderr)
            self.assertNotIn('a'*32,result.stdout+result.stderr)
            recipe = (work/'dependencies.Dockerfile').read_text()
            self.assertIn('baseurl=https://dl.rockylinux.org/pub/rocky/8/', recipe)
            self.assertLess(recipe.index('for mapping'), recipe.index('dnf -y install'))
            self.assertNotIn('gpgcheck=0', recipe)
            self.assertNotIn('sslverify=0', recipe)
            manifest = json.loads((work/'release/release.json').read_text())
            self.assertEqual(manifest['binary_sha256'],hashlib.sha256((work/'release/AyuGram Inline Test').read_bytes()).hexdigest())
            self.assertEqual(manifest['patch_sha256'],hashlib.sha256((context/'ayugram-inline.patch').read_bytes()).hexdigest())
            self.assertTrue((work/'ayugram-linux-x86_64.tar.gz').is_file())
            self.assertIn('--cpuset-cpus=0-7 --memory=24g',trace.read_text())
            self.assertNotIn('gcloud',trace.read_text())
            result = subprocess.run(['bash',str(context/'build_linux.sh'),str(work)],
                                    env=env,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)


if __name__ == '__main__':
    unittest.main()
