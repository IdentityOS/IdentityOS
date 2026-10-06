"""Reproduce legacy CLI publishing/install behavior in an isolated file tree."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile

ROOT=Path(__file__).resolve().parents[2]

def inspect():
    with tempfile.TemporaryDirectory(prefix='idos-registry-gap-') as temp:
        root=Path(temp)
        (root/'cli').mkdir()
        shutil.copyfile(ROOT/'cli/registry_cmds.py',root/'cli/registry_cmds.py')
        spec=importlib.util.spec_from_file_location('isolated_registry',root/'cli/registry_cmds.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manifest=root/'registry/identities/creator/poet/manifest.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({'id':'creator/poet','name':'Poet'}))
        index=root/'registry/index.json'
        index.write_text(json.dumps({'registry':'','identities':[{'id':'creator/poet','url':'identities/creator/poet/manifest.json'}]}))
        before=index.read_bytes()
        captured=io.StringIO()
        with contextlib.redirect_stdout(captured):
            module.cmd_registry_publish(str(manifest))
        try:
            module.cmd_registry_install('creator/poet')
            install_error=None
        except Exception as exc:
            install_error=f'{type(exc).__name__}: {exc}'
        return {'legacy_publish_output':captured.getvalue(),
                'legacy_publish_index_unchanged':before==index.read_bytes(),
                'namespaced_install_error':install_error,
                'namespaced_install_file_exists':(root/'.identity_store/creator/poet.json').exists()}

if __name__=='__main__':
    print(json.dumps(inspect(),indent=2))
