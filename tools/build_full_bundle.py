"""Build a local Full Bundle from Core source and verified official module ZIPs.

No downloads, dependency installation, commits or publication are performed.
"""
import argparse
import ast
from datetime import date
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile


ROOT_FILES = {
    'hub.py', 'requirements.txt', 'start.sh', 'start.bat', 'LICENSE.md',
    'THIRD-PARTY-NOTICES.md', 'CONTRIBUTING.md', 'DATA-ASSETS.md',
    'Dockerfile', 'docker-compose.yml', '.dockerignore',
}
METADATA = {'resources/cyberhub-package.json', 'cyberhub-package.json'}
BLOCKED_NAMES = {'settings.json', 'installed-modules.json', 'update-notifications.json',
                 '.cyberhub.lock'}
CORE_ADDITIONS = (
    'core/update_notifications.py',
    'licenses/Danbooru-Autotagger-MIT.txt',
    'licenses/Real-ESRGAN-BSD-3-Clause.txt',
    'resources/provenance/danbooru-autotagger.json',
    'resources/provenance/prompting-guides.json',
)
PREFIX = 'CyberHub-Full-Bundle/'


def dump(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, text=True).strip()


def safe_member(name):
    path = PurePosixPath(name)
    if (not name or '\\' in name or path.is_absolute() or path.as_posix() != name
            or re.match(r'^[A-Za-z]:', name) or '..' in path.parts):
        raise ValueError(f'Unsafe package path: {name}')
    if (any(part.startswith('.') or part in {'__pycache__', 'venv', 'env'} for part in path.parts)
            and name != '.dockerignore'):
        raise ValueError(f'Local/private package path: {name}')
    if (path.name in BLOCKED_NAMES
            or name.lower().endswith(('.db-wal', '.db-shm', '.sqlite-wal', '.sqlite-shm'))
            or path.suffix.lower() in {'.pyc', '.pyo', '.db', '.sqlite', '.sqlite3'}):
        raise ValueError(f'Runtime data in package: {name}')
    return name


def add_file(files, origins, name, data, origin):
    safe_member(name)
    if name in files and files[name] != data:
        raise ValueError(f'Conflicting bundle file {name}: {origins[name]} versus {origin}')
    files[name] = data
    origins.setdefault(name, origin)


def class_values(path, class_name=None):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and (node.name == class_name if class_name else
                    any(isinstance(base, ast.Name) and base.id == 'Module' for base in node.bases)))
    return {node.targets[0].id: ast.literal_eval(node.value) for node in cls.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {'VERSION', 'version', 'release_stage'}}


def source_record(repo):
    return {'repository': f'cyberdeliaAI/{repo.name}',
            'commit': git(repo, 'rev-parse', 'HEAD'),
            'working_tree_modified': bool(git(repo, 'status', '--porcelain'))}


def verify_provenance(files):
    model = json.loads(files['resources/provenance/danbooru-autotagger.json'])
    guides = json.loads(files['resources/provenance/prompting-guides.json'])
    for item in [model['model'], model['labels'], *guides['files']]:
        if digest(files[item['path']]) != item['sha256']:
            raise ValueError(f"Provenance hash mismatch: {item['path']}")


def find_archive(workspace, module):
    repo = module['repository'].split('/')[-1]
    for directory in (workspace / '_release-assets' / repo, workspace / '_build'):
        for candidate in sorted(directory.glob('**/' + module['asset'])):
            if candidate.is_file() and candidate.stat().st_size == module['size']:
                if digest(candidate.read_bytes()) == module['sha256']:
                    return candidate
    raise ValueError(f"No locally verified archive for {module['id']} {module['version']}")


def add_module(workspace, module, declared, files, origins):
    module_id = module['id']
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', module_id):
        raise ValueError(f'Invalid module ID: {module_id}')
    if module.get('publisher_type') != 'official' or not module['repository'].startswith('cyberdeliaAI/'):
        raise ValueError(f'Not an official module: {module_id}')
    repo = workspace / module['repository'].split('/')[-1]
    local = json.loads((repo / 'module.json').read_text(encoding='utf-8'))
    for key in ('id', 'version', 'channel', 'repository', 'minimum_hub_version', 'dependencies', 'python_dependencies'):
        if local.get(key) != module.get(key):
            raise ValueError(f'{module_id}: source and catalog disagree on {key}')
    values = class_values(repo / 'modules' / module_id / '__init__.py')
    if (values.get('version', '1.0') != module['version']
            or values.get('release_stage', 'stable') != module['channel']):
        raise ValueError(f'{module_id}: class and manifest version/channel disagree')
    archive = find_archive(workspace, module)
    with zipfile.ZipFile(archive) as zf:
        if zf.testzip() is not None or len(zf.namelist()) != len(set(zf.namelist())):
            raise ValueError(f'{module_id}: corrupt archive or duplicate paths')
        metadata = [name for name in zf.namelist() if name in METADATA]
        if len(metadata) != 1:
            raise ValueError(f'{module_id}: expected one package manifest')
        packaged = json.loads(zf.read(metadata[0]))
        for key in ('product', 'type', 'id', 'version', 'channel', 'repository', 'minimum_hub_version'):
            if packaged.get(key) != local.get(key):
                raise ValueError(f'{module_id}: archive manifest disagrees on {key}')
        members = {}
        for info in zf.infolist():
            if info.is_dir() or info.filename in METADATA:
                continue
            name = safe_member(info.filename)
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError(f'{module_id}: symlink in archive')
            if not name.startswith((f'modules/{module_id}/', 'resources/', 'licenses/')):
                raise ValueError(f'{module_id}: unexpected archive scope: {name}')
            members[name] = zf.read(info)
        # Validate every tracked runtime file against source, including the
        # prompt/resource trees that are not enumerated in module-sources.json.
        for name in git(repo, 'ls-files').splitlines():
            if name.startswith((f'modules/{module_id}/', 'resources/')):
                if members.get(name) != (repo / name).read_bytes():
                    raise ValueError(f'{module_id}: packaged runtime differs from source: {name}')
        for name in declared.get('resources', []):
            if name not in members:
                raise ValueError(f'{module_id}: missing declared resource: {name}')
        for name, data in members.items():
            add_file(files, origins, name, data, f'{archive.name}!/{name}')
    # License notices may be completed locally without republishing otherwise
    # identical module runtime archives. Conflicting license bytes still fail.
    for path in sorted((repo / 'licenses').rglob('*')):
        if path.is_file():
            if path.is_symlink():
                raise ValueError(f'{module_id}: symlink in source licenses')
            add_file(files, origins, path.relative_to(repo).as_posix(),
                     path.read_bytes(), f'{repo.name}/{path.relative_to(repo).as_posix()}')
    for name in ('LICENSE.md', 'THIRD-PARTY-NOTICES.md'):
        path = repo / name
        if path.is_file():
            add_file(files, origins, f'licenses/modules/{module_id}/{name}', path.read_bytes(), f'{repo.name}/{name}')
    return {**local, 'source': source_record(repo),
            'archive': {'filename': archive.name, 'sha256': module['sha256'], 'size': module['size']}}


def build_bundle(core, output, build_date):
    core, output = Path(core).resolve(), Path(output).resolve()
    workspace = core.parent
    date.fromisoformat(build_date)
    version = class_values(core / 'hub.py', 'Hub')['VERSION']
    declarations = json.loads((core / 'module-sources.json').read_text())
    catalog_path = workspace / 'CyberHub-Registry/cyberhub-registry.json'
    catalog = json.loads(catalog_path.read_text())
    declared = {item['id']: item for item in declarations['modules'] if item['publisher_type'] == 'official'}
    modules = {item['id']: item for item in catalog['modules'] if item['publisher_type'] == 'official'}
    if (set(declared) != set(modules) or len(modules) != len(catalog['modules'])
            or len(modules) != len(declarations['modules'])):
        raise ValueError('Official module inventories differ or contain duplicate IDs')
    for module_id, item in modules.items():
        if item['repository'] != declared[module_id]['repository']:
            raise ValueError(f'{module_id}: catalog points to an unexpected repository')
        if set(item.get('dependencies', [])) - set(modules):
            raise ValueError(f'{module_id}: missing bundle dependency')
    files, origins = {}, {}
    for name in git(core, 'ls-files').splitlines():
        if name in ROOT_FILES or name.startswith(('core/', 'modules/settings/', 'modules/module_manager/', 'resources/', 'licenses/')):
            path = core / name
            if path.is_symlink():
                raise ValueError(f'Symlink in Core source: {name}')
            add_file(files, origins, name, path.read_bytes(), f'CyberHub/{name}')
    # Approved additions are explicitly included before they are committed.
    for name in CORE_ADDITIONS:
        add_file(files, origins, name, (core / name).read_bytes(), f'CyberHub/{name}')
    inventory = [add_module(workspace, modules[module_id], declared[module_id], files, origins)
                 for module_id in declared]
    verify_provenance(files)
    table = '\n'.join(f"| {item['name']} | {item['version']} | {item['channel']} |" for item in inventory)
    guide = (core / 'FULL-BUNDLE.md').read_text(encoding='utf-8')
    guide = guide.replace('{{VERSION}}', version).replace('{{BUILD_DATE}}', build_date).replace('{{MODULE_TABLE}}', table)
    info = {'schema': 1, 'product': 'CyberHub', 'distribution': 'full-bundle',
            'version': version, 'build_date': build_date, 'published': False,
            'publication_target': 'Civitai',
            'core_source': source_record(core), 'modules': inventory,
            'registry_sha256': digest(catalog_path.read_bytes()),
            'system_components': {'settings': class_values(core / 'modules/settings/__init__.py')['version'],
                                  'module_manager': class_values(core / 'modules/module_manager/__init__.py')['version']}}
    generated = {
        'README.md': guide.encode(),
        # This root marker is intentionally outside older Core import allowlists,
        # so they also reject a Full Bundle before replacing installed modules.
        'FULL-BUNDLE.json': dump({'product': 'CyberHub', 'version': version,
                                 'distribution': 'full-bundle', 'fresh_install_only': True}),
        'cyberhub-version.txt': f'CyberHub {version}\n'.encode(),
        'resources/bundle.json': dump(info),
        'resources/module-sources.json': (core / 'module-sources.json').read_bytes(),
        'resources/CHANGELOG.md': (core / 'CHANGELOG.md').read_bytes(),
        'resources/release.json': dump({'product': 'CyberHub', 'version': version, 'distribution': 'full-bundle', 'test_build': build_date}),
        'resources/cyberhub-package.json': dump({'schema': 2, 'product': 'CyberHub', 'type': 'hub',
            'id': 'cyberhub', 'name': 'CyberHub FULL Bundle', 'version': version,
            'minimum_hub_version': version, 'repository': 'cyberdeliaAI/CyberHub',
            'publisher_type': 'official', 'channel': 'beta', 'distribution': 'full-bundle',
            'fresh_install_only': True, 'test_build': build_date, 'bundled_modules': list(declared)}),
    }
    for name, data in generated.items():
        add_file(files, origins, name, data, 'generated bundle metadata/guide')
    for name, data in files.items():
        if name.endswith('.py'):
            compile(data, name, 'exec')
    if not all(name in files for name in ('start.sh', 'start.bat', 'requirements.txt', 'LICENSE.md', 'licenses/GPL-3.0.txt')):
        raise ValueError('Required startup or license file is missing')
    if any(name.startswith('resources/auto_tagger/') and name.endswith(('.onnx', '.csv')) for name in files):
        raise ValueError('Auto Tagger optional model must remain a user-triggered download')
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f'cyberhub_full_bundle_v{version}_test_{build_date}.zip'
    zip_date = (*date.fromisoformat(build_date).timetuple()[:3], 0, 0, 0)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(PREFIX + name, zip_date)
            entry.create_system = 3
            entry.external_attr = (0o100755 if name == 'start.sh' else 0o100644) << 16
            zf.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    with zipfile.ZipFile(destination) as zf:
        assert zf.testzip() is None
        assert len(zf.namelist()) == len(files)
        for name, data in files.items():
            assert zf.read(PREFIX + name) == data, name
    sha256 = digest(destination.read_bytes())
    destination.with_suffix('.zip.sha256').write_text(f'{sha256}  {destination.name}\n')
    report = {'archive': destination.name, 'sha256': sha256, 'bytes': destination.stat().st_size,
              'files': {name: {'sha256': digest(data), 'bytes': len(data), 'origin': origins[name]}
                        for name, data in sorted(files.items())}, 'bundle': info}
    (output / 'build-report.json').write_bytes(dump(report))
    (output / 'README.md').write_bytes(generated['README.md'])
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--build-date', required=True, help='YYYY-MM-DD; explicitly identifies this unpublished test build')
    args = parser.parse_args()
    print(build_bundle(Path(__file__).resolve().parents[1], args.output, args.build_date))
