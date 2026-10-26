#!/usr/bin/env python3
"""Compile the shareable Chinese handbook with XeLaTeX (run from any directory)."""
import shutil
import subprocess
import os
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / 'docs' / 'guide'
NAME = '服务器监控入门指南'


def main():
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import DecodedStreamObject, NameObject
    except ImportError:
        raise SystemExit('请先安装文档构建依赖：python3 -m pip install -r tools/requirements-docs.txt')
    engine = shutil.which('xelatex')
    if engine is None:
        raise SystemExit('需要安装带有 ctex、Fandol 字体的 TeX Live / MacTeX，并使 xelatex 可用。')
    output = PROJECT / '.build'
    output.mkdir(exist_ok=True)
    command = [engine, '-interaction=nonstopmode', '-halt-on-error',
               '-output-directory=' + str(output), '-jobname=' + NAME, 'main.tex']
    env = os.environ.copy()
    # Search real directories, including installations with a stale TeX database.
    lookup = shutil.which('kpsewhich')
    if lookup:
        dist = subprocess.run([lookup, '-var-value=TEXMFDIST'], capture_output=True, text=True)
        cmap = Path(dist.stdout.strip()) / 'fonts' / 'cmap'
        if dist.returncode == 0 and cmap.is_dir():
            env['CMAPFONTS'] = str(cmap) + '//' + os.pathsep + env.get('CMAPFONTS', '')
    for _ in range(2):
        result = subprocess.run(command, cwd=PROJECT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env)
        if result.returncode:
            print(result.stdout[-12000:])
            raise SystemExit(result.returncode)
    target = PROJECT / (NAME + '.pdf')
    # CID-keyed Fandol fonts can omit ToUnicode and rely on a viewer's language
    # pack. Embed Adobe's standard map so the PDF also renders/searches/copies in
    # viewers without that pack. Preserve the original TeX layout and bookmarks.
    reader = PdfReader(output / target.name)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    map_ref = None
    seen = set()
    for page in writer.pages:
        for ref in page['/Resources'].get('/Font', {}).values():
            font = ref.get_object()
            if id(font) in seen:
                continue
            seen.add(id(font))
            descendants = font.get('/DescendantFonts')
            if not descendants or font.get('/ToUnicode'):
                continue
            info = descendants[0].get_object().get('/CIDSystemInfo', {})
            if info.get('/Registry') != 'Adobe' or info.get('/Ordering') != 'GB1':
                continue
            if map_ref is None:
                if not lookup:
                    raise SystemExit('需要 kpsewhich 和 Adobe-GB1-UCS2 CMap（TeX Live adobemapping 包）。')
                result = subprocess.run([lookup, '-format=cmap', 'Adobe-GB1-UCS2'],
                                        capture_output=True, text=True, env=env)
                mapping = Path(result.stdout.strip())
                if result.returncode or not mapping.is_file():
                    raise SystemExit('找不到 Adobe-GB1-UCS2；请安装 TeX Live 的 adobemapping 包。')
                stream = DecodedStreamObject()
                stream.set_data(mapping.read_bytes())
                map_ref = writer._add_object(stream)
            font[NameObject('/ToUnicode')] = map_ref
    with target.open('wb') as handle:
        writer.write(handle)
    if '服务器占用可视化' not in PdfReader(target).pages[0].extract_text():
        raise SystemExit('PDF 中文文字提取验证失败，请检查字体映射。')
    print(target)
    bundle = ROOT / 'dist' / '服务器监控手册-TeX源码.zip'
    bundle.parent.mkdir(exist_ok=True)
    files = {
        'docs/guide/README.md': 'README.md',
        'docs/guide/main.tex': 'docs/guide/main.tex',
        'tools/build_guide.py': 'tools/build_guide.py',
        'tools/requirements-docs.txt': 'tools/requirements-docs.txt',
    }
    for figure in sorted((PROJECT / 'images').glob('*.jpg')):
        relative = figure.relative_to(ROOT).as_posix()
        files[relative] = relative
    with ZipFile(bundle, 'w', ZIP_DEFLATED) as archive:
        for source, name in files.items():
            archive.write(ROOT / source, 'GuideSource/' + name)
    print(bundle)


if __name__ == '__main__':
    main()
