"""Build Danbooru/Upscaler notice-only patch candidates from verified old ZIPs.

Large runtime assets remain byte-identical. Only the version constant, package
metadata, notices, license texts and provenance records may change. No downloads
or publication occur, and the Registry is not modified by this builder.
"""
import argparse
import ast
import json
from pathlib import Path
import zipfile

from build_full_bundle import (
    METADATA, add_file, class_values, digest, dump, find_archive, git, safe_member, source_record,
)
from build_core_packages import write_archive


def without_module_version(source):
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and any(
                isinstance(base, ast.Name) and base.id == 'Module' for base in node.bases):
            node.body = [item for item in node.body if not (
                isinstance(item, ast.Assign) and len(item.targets) == 1
                and isinstance(item.targets[0], ast.Name) and item.targets[0].id == 'version')]
    return ast.dump(tree, include_attributes=False)


def build_notice_update(workspace, baseline, module_id, output, build_date):
    if module_id not in {'danbooru', 'upscaler'}:
        raise ValueError('Only the two reviewed notice patches are supported')
    old = next(item for item in baseline['modules'] if item['id'] == module_id)
    repo = Path(workspace) / old['repository'].split('/')[-1]
    manifest = json.loads((repo / 'module.json').read_text())
    for key in ('id', 'type', 'name', 'repository', 'channel', 'dependencies',
                'minimum_hub_version', 'python_dependencies', 'publisher_type'):
        expected = 'module' if key == 'type' else old[key]
        if manifest.get(key) != expected:
            raise ValueError(f'{module_id}: notice patch changed {key}')
    if manifest['version'] == old['version']:
        raise ValueError('A notice patch requires a new version')
    source_name = f'modules/{module_id}/__init__.py'
    values = class_values(repo / source_name)
    if values.get('version') != manifest['version'] or values.get('release_stage', 'stable') != manifest['channel']:
        raise ValueError('Class version/channel disagrees with module.json')
    archive = find_archive(Path(workspace), old)
    files, origins = {}, {}
    with zipfile.ZipFile(archive) as zf:
        if zf.testzip() is not None or len(zf.namelist()) != len(set(zf.namelist())):
            raise ValueError('Invalid baseline archive')
        metadata = [name for name in zf.namelist() if name in METADATA]
        if len(metadata) != 1:
            raise ValueError('Expected one baseline manifest')
        packaged = json.loads(zf.read(metadata[0]))
        for key in ('id', 'version', 'repository', 'channel'):
            if packaged.get(key) != old.get(key):
                raise ValueError(f'Baseline manifest disagrees on {key}')
        for info in zf.infolist():
            if info.is_dir() or info.filename in METADATA:
                continue
            name = safe_member(info.filename)
            if not name.startswith((f'modules/{module_id}/', 'resources/', 'licenses/')):
                raise ValueError(f'Unexpected baseline path: {name}')
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError('Symlink in baseline archive')
            add_file(files, origins, name, zf.read(info), f'{archive.name}!/{name}')
    if without_module_version(files[source_name]) != without_module_version((repo / source_name).read_bytes()):
        raise ValueError('Notice patch contains functional Python changes')
    for name in git(repo, 'ls-files').splitlines():
        if name.startswith((f'modules/{module_id}/', 'resources/')):
            if name == source_name or name.startswith('resources/provenance/'):
                continue
            if files.get(name) != (repo / name).read_bytes():
                raise ValueError(f'Notice patch contains changed runtime/resource bytes: {name}')
    files[source_name] = (repo / source_name).read_bytes()
    origins[source_name] = f'{repo.name}/{source_name}'
    for name in ('LICENSE.md', 'THIRD-PARTY-NOTICES.md'):
        add_file(files, origins, f'licenses/modules/{module_id}/{name}',
                 (repo / name).read_bytes(), f'{repo.name}/{name}')
    for path in sorted((repo / 'licenses').rglob('*')):
        if path.is_file():
            if path.is_symlink():
                raise ValueError('Symlink in source licenses')
            name = path.relative_to(repo).as_posix()
            add_file(files, origins, name, path.read_bytes(), f'{repo.name}/{name}')
    if module_id == 'danbooru':
        for leaf in ('danbooru-autotagger.json', 'prompting-guides.json'):
            name = f'resources/provenance/{leaf}'
            add_file(files, origins, name, (repo / name).read_bytes(), f'{repo.name}/{name}')
        from build_full_bundle import verify_provenance
        verify_provenance(files)
    files['resources/cyberhub-package.json'] = dump({**manifest, 'schema': 2})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"cyberhub_module_{module_id}_v{manifest['version']}.zip"
    record = write_archive(destination, files, build_date)
    entry = {**old, 'version': manifest['version'], 'asset': record['asset'],
             'sha256': record['sha256'], 'size': record['size'],
             'download_url': f"https://github.com/{manifest['repository']}/releases/download/v{manifest['version']}/{record['asset']}"}
    report = {'published': False, 'module': manifest, 'source': source_record(repo),
              'baseline': old, 'package': record, 'catalog_entry': entry,
              'functional_ast_unchanged_except_version': True,
              'files': {n: {'sha256': digest(data), 'bytes': len(data),
                            'origin': origins.get(n, 'generated manifest')}
                        for n, data in files.items()}}
    (output / f'{module_id}-build-report.json').write_bytes(dump(report))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-registry', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--build-date', required=True)
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[2]
    baseline = json.loads(Path(args.baseline_registry).read_text())
    results = [build_notice_update(workspace, baseline, module_id, args.output, args.build_date)
               for module_id in ('danbooru', 'upscaler')]
    print(json.dumps([r['package'] for r in results], indent=2))
