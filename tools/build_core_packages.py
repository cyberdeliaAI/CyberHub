"""Build Lite and system-update packages locally from the active Core.

Uses a locally verified Gallery archive, installs nothing and publishes nothing.
"""
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import zipfile

from build_full_bundle import (
    CORE_ADDITIONS, METADATA, ROOT_FILES, add_file, class_values, digest, dump,
    find_archive, git, safe_member, source_record,
)


def write_archive(destination, files, build_date):
    stamp = (*date.fromisoformat(build_date).timetuple()[:3], 0, 0, 0)
    for name, data in files.items():
        safe_member(name)
        if name.endswith('.py'):
            compile(data, name, 'exec')
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, stamp)
            info.create_system = 3
            info.external_attr = (0o100755 if name == 'start.sh' else 0o100644) << 16
            zf.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    with zipfile.ZipFile(destination) as zf:
        if zf.testzip() is not None or len(zf.namelist()) != len(set(zf.namelist())):
            raise ValueError('Invalid ZIP output')
        for name, data in files.items():
            if zf.read(name) != data:
                raise ValueError(f'Packaged bytes differ: {name}')
    sha256 = digest(destination.read_bytes())
    destination.with_suffix('.zip.sha256').write_text(f'{sha256}  {destination.name}\n')
    return {'asset': destination.name, 'sha256': sha256,
            'size': destination.stat().st_size, 'files': len(files)}


def core_files(core):
    files, origins = {}, {}
    for name in git(core, 'ls-files').splitlines():
        if name.startswith('resources/provenance/'):
            continue
        if name in ROOT_FILES or name.startswith((
                'core/', 'modules/settings/', 'modules/module_manager/', 'resources/', 'licenses/')):
            path = core / name
            if path.is_symlink():
                raise ValueError(f'Symlink in Core source: {name}')
            add_file(files, origins, name, path.read_bytes(), f'CyberHub/{name}')
    # The other approved additions describe assets owned by the Full/Danbooru
    # packages. They are intentionally absent from the basic/system package.
    for name in CORE_ADDITIONS:
        if name.startswith('resources/provenance/'):
            continue
        add_file(files, origins, name, (core / name).read_bytes(), f'CyberHub/{name}')
    for name in ('resources/cyberhub-package.json', 'resources/bundle.json',
                 'resources/distribution.json', 'resources/release.json'):
        files.pop(name, None)
        origins.pop(name, None)
    return files, origins


def gallery_files(workspace, catalog):
    module = next(item for item in catalog['modules'] if item['id'] == 'gallery')
    repo = workspace / module['repository'].split('/')[-1]
    manifest = json.loads((repo / 'module.json').read_text())
    for key in ('id', 'version', 'channel', 'repository', 'dependencies', 'python_dependencies'):
        if manifest.get(key) != module.get(key):
            raise ValueError(f'Gallery source/catalog mismatch: {key}')
    archive = find_archive(workspace, module)
    files, origins = {}, {}
    with zipfile.ZipFile(archive) as zf:
        if zf.testzip() is not None or len(zf.namelist()) != len(set(zf.namelist())):
            raise ValueError('Invalid Gallery archive')
        metadata = [n for n in zf.namelist() if n in METADATA]
        if len(metadata) != 1:
            raise ValueError('Expected one Gallery manifest')
        packaged = json.loads(zf.read(metadata[0]))
        for key in ('id', 'type', 'version', 'channel', 'repository'):
            if packaged.get(key) != manifest.get(key):
                raise ValueError(f'Gallery archive mismatch: {key}')
        for info in zf.infolist():
            if info.is_dir() or info.filename in METADATA:
                continue
            name = safe_member(info.filename)
            if not name.startswith(('modules/gallery/', 'resources/', 'licenses/')):
                raise ValueError(f'Unexpected Gallery archive path: {name}')
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError('Symlink in Gallery archive')
            add_file(files, origins, name, zf.read(info), f'{archive.name}!/{name}')
    for name in git(repo, 'ls-files').splitlines():
        if name.startswith(('modules/gallery/', 'resources/')):
            if files.get(name) != (repo / name).read_bytes():
                raise ValueError(f'Gallery runtime differs from verified archive: {name}')
    for name in ('LICENSE.md', 'THIRD-PARTY-NOTICES.md'):
        add_file(files, origins, f'licenses/modules/gallery/{name}',
                 (repo / name).read_bytes(), f'{repo.name}/{name}')
    return files, origins, {**manifest, 'archive': module['asset'],
                            'archive_sha256': module['sha256'], 'source': source_record(repo)}


def build_packages(core, output, build_date):
    core, output = Path(core).resolve(), Path(output).resolve()
    date.fromisoformat(build_date)
    output.mkdir(parents=True, exist_ok=True)
    version = class_values(core / 'hub.py', 'Hub')['VERSION']
    catalog = json.loads((core.parent / 'CyberHub-Registry/cyberhub-registry.json').read_text())
    base, origins = core_files(core)
    shared = {
        'cyberhub-version.txt': f'CyberHub {version}\n'.encode(),
        'resources/release.json': dump({'product': 'CyberHub', 'version': version}),
        'resources/CHANGELOG.md': (core / 'CHANGELOG.md').read_bytes(),
        'resources/module-sources.json': (core / 'module-sources.json').read_bytes(),
    }
    for name, data in shared.items():
        add_file(base, origins, name, data, 'generated/current source')
    manifest = {'schema': 2, 'product': 'CyberHub', 'type': 'hub', 'id': 'cyberhub',
                'name': 'CyberHub system', 'version': version, 'minimum_hub_version': '',
                'repository': 'cyberdeliaAI/CyberHub', 'publisher_type': 'official',
                'channel': 'stable', 'dependencies': [], 'python_dependencies': []}
    system = {**base, 'README.md': (core / 'README.md').read_bytes(),
              'resources/cyberhub-package.json': dump(manifest)}
    if any(n.startswith('modules/') and not n.startswith((
            'modules/settings/', 'modules/module_manager/')) for n in system):
        raise ValueError('Optional module in system update')
    gallery, gallery_origins, gallery_info = gallery_files(core.parent, catalog)
    lite, lite_origins = dict(base), dict(origins)
    for name, data in gallery.items():
        add_file(lite, lite_origins, name, data, gallery_origins[name])
    guide = (core / 'LITE-BUNDLE.md').read_text()
    for before, after in [('{{VERSION}}', version), ('{{BUILD_DATE}}', build_date),
                          ('{{GALLERY_VERSION}}', gallery_info['version'])]:
        guide = guide.replace(before, after)
    lite.update({
        'README.md': guide.encode(),
        'LITE-BUNDLE.json': dump({'product': 'CyberHub', 'version': version,
                                  'distribution': 'lite', 'fresh_install_only': True}),
        'resources/cyberhub-package.json': dump({**manifest, 'name': 'CyberHub Lite',
            'distribution': 'lite', 'fresh_install_only': True, 'bundled_modules': ['gallery']}),
        'resources/distribution.json': dump({'product': 'CyberHub', 'version': version,
            'distribution': 'lite', 'publication_target': 'GitHub',
            'build_date': build_date, 'core_source': source_record(core),
            'settings': class_values(core / 'modules/settings/__init__.py')['version'],
            'module_manager': class_values(core / 'modules/module_manager/__init__.py')['version'],
            'gallery': gallery_info}),
    })
    records = {}
    for kind, files in [('lite', lite), ('system', system)]:
        name = f'cyberhub_lite_v{version}.zip' if kind == 'lite' else f'cyberhub_update_v{version}.zip'
        records[kind] = write_archive(output / name, files, build_date)
    update = {'schema': 1, 'product': 'CyberHub', 'repository': 'cyberdeliaAI/CyberHub',
              'release_version': version,
              'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'packages': [{k: v for k, v in {**manifest, **records['system']}.items()
                            if k in {'type', 'id', 'name', 'version', 'minimum_hub_version',
                                     'asset', 'size', 'sha256'}}]}
    (output / 'cyberhub-update.json').write_bytes(dump(update))
    (output / 'packages.json').write_bytes(dump(records))
    (output / 'core-build-report.json').write_bytes(dump({
        'publication_target': 'GitHub', 'core_source': source_record(core),
        'packages': records, 'files': {
            kind: {n: {'sha256': digest(data), 'bytes': len(data)} for n, data in files.items()}
            for kind, files in [('lite', lite), ('system', system)]},
        'gallery': gallery_info}))
    return records


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--build-date', required=True)
    args = parser.parse_args()
    print(json.dumps(build_packages(Path(__file__).resolve().parents[1], args.output, args.build_date), indent=2))
