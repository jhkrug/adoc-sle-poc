#!/usr/bin/env python3
"""Compare the words of the DocBook book (SLES profile) with the Antora HTML.

Baseline: lxml resolves entities and XIncludes, applies the SLES profile,
drops metadata, and replaces each xref with the target title (DocBook
generates xref text at render time, so the source has none).
Candidate: text of <article class="doc"> for each page, in nav order.
"""
import argparse
import difflib
import json
import re
from pathlib import Path

from lxml import etree, html

from dbcommon import DB, XMLID, SLES_PROFILE, ln, profile_filter

MODULE = 'rmt'
DROP = {'revhistory', 'meta', 'date', 'titleabbrev', 'remark', 'indexterm', 'docmanager'}
DROP_IN_INFO = {'productname', 'productnumber'}
BLOCKISH = {'para', 'simpara', 'title', 'term', 'entry', 'screen', 'step', 'listitem',
            'member', 'question', 'answer', 'row', 'varlistentry', 'callout', 'textobject',
            'imageobject', 'mediaobject', 'note', 'tip', 'warning', 'important', 'caution',
            'abstract', 'legalnotice', 'formalpara', 'bridgehead', 'figure', 'table',
            'example', 'procedure', 'itemizedlist', 'orderedlist', 'variablelist',
            'substeps', 'chapter', 'section', 'sect1', 'sect2', 'sect3', 'sect4',
            'appendix', 'preface', 'part', 'book', 'info', 'qandaentry', 'simplelist'}
KEYS = {'control': 'Ctrl', 'alt': 'Alt', 'shift': 'Shift', 'enter': 'Enter',
        'escape': 'Esc', 'tab': 'Tab', 'delete': 'Del', 'backspace': 'Backspace',
        'space': 'Space', 'meta': 'Meta'}


def q(t):
    return f'{{{DB}}}{t}'


def load_profiled(src, fn):
    """Like DAPS: profile each file first, then resolve its XIncludes.

    lxml's xinclude() drops the attributes of <xi:include>, so a profiled
    include (os="sled") would be kept. DAPS profiles before it resolves.
    """
    parser = etree.XMLParser(load_dtd=True, resolve_entities=True, no_network=True)
    root = etree.parse(str(Path(src) / 'xml' / fn), parser).getroot()
    profile_filter(root, SLES_PROFILE)
    for inc in list(root.iter('{http://www.w3.org/2001/XInclude}include')):
        child = load_profiled(src, inc.get('href'))
        child.tail = inc.tail
        inc.getparent().replace(inc, child)
    return root


def baseline(src, book):
    root = load_profiled(src, book)
    ids = {el.get(XMLID): el for el in root.iter() if isinstance(el.tag, str) and el.get(XMLID)}
    for el in list(root.iter()):
        if not isinstance(el.tag, str):           # comments, PIs
            if el.getparent() is not None:
                tail = el.tail
                p = el.getparent()
                prev = el.getprevious()
                if prev is not None:
                    prev.tail = (prev.tail or '') + (tail or '')
                else:
                    p.text = (p.text or '') + (tail or '')
                p.remove(el)
            continue
        if ln(el) == 'keycap' and not ''.join(el.itertext()).strip():
            f = el.get('function', '')
            el.text = KEYS.get(f, f)
        if ln(el) in DROP or (ln(el) in DROP_IN_INFO and ln(el.getparent()) == 'info'):
            tail = el.tail
            el.clear()
            el.tail = tail
        elif ln(el) == 'xref':
            t = ids.get(el.get('linkend'))
            title = ''
            if t is not None:
                tt = t.find(q('title'))
                if tt is None and t.find(q('info')) is not None:
                    tt = t.find(q('info')).find(q('title'))
                if tt is not None:
                    title = ''.join(tt.itertext())
            el.text = title
        elif ln(el) == 'link' and not ''.join(el.itertext()).strip():
            el.text = el.get('{http://www.w3.org/1999/xlink}href')
        elif ln(el) == 'menuchoice':
            for c in el:
                c.tail = ' ' + (c.tail or '')
        elif ln(el) == 'keycombo':
            for c in el[:-1]:
                c.tail = '+'
            el[-1].tail = el[-1].tail or ''
        if ln(el) in BLOCKISH:          # word break around block elements only
            el.tail = ' ' + (el.tail or '')
            el.text = ' ' + (el.text or '')
    return ''.join(root.itertext())


STRUCT_DB = {
    'screens': ['screen'],
    'sections': ['sect1', 'sect2', 'sect3', 'sect4', 'section', 'bridgehead'],
    'ordered items': ['orderedlist/listitem', 'procedure/step', 'substeps/step'],
    'bullet items': ['itemizedlist/listitem', 'simplelist/member', 'stepalternatives/step'],
    'terms': ['varlistentry', 'qandaentry/question'],
    'admonitions': ['note', 'tip', 'warning', 'important', 'caution'],
    'images': ['figure', 'informalfigure'],
    'table rows': ['row'],
}
STRUCT_HTML = {
    'screens': './/pre',
    'sections': './/h2|.//h3|.//h4|.//h5',
    'ordered items': './/ol/li[not(parent::ol/parent::div[contains(@class,"qlist")])]',
    'bullet items': './/ul/li',
    'terms': './/dt|.//div[contains(@class,"qlist")]/ol/li',
    'admonitions': './/div[contains(@class,"admonitionblock")]',
    'images': './/div[contains(@class,"imageblock")]',
    'table rows': './/tr[not(ancestor::div[contains(@class,"admonitionblock")])]'
                  '[not(ancestor::div[contains(@class,"colist")])]',
}


def struct_db(src, book):
    root = load_profiled(src, book)
    ns = {'d': DB}
    res = {}
    for k, paths in STRUCT_DB.items():
        res[k] = sum(len(root.xpath('//' + '/'.join('d:' + x for x in p.split('/')),
                                    namespaces=ns)) for p in paths)
    return res


def struct_html(site, docs):
    pages = nav_pages(docs)
    res = dict.fromkeys(STRUCT_HTML, 0)
    for p in pages:
        f = Path(site) / 'sles' / '15-SP7' / MODULE / f'{p}.html'
        art = html.parse(str(f)).getroot().xpath('//article[contains(@class,"doc")]')[0]
        for k, xp in STRUCT_HTML.items():
            res[k] += len(art.xpath(xp))
    return res


SLES_ATTRS = {'os-sles', 'cond-suse-product'} | {f'arch-{a}' for a in SLES_PROFILE['arch']}


def nav_pages(docs):
    """Pages in nav.adoc, with ifdef blocks evaluated for the SLES profile."""
    pages, stack = [], []
    for line in Path(docs, 'modules', MODULE, 'nav.adoc').read_text().splitlines():
        m = re.match(r'ifdef::([^\[]+)\[\]$', line)
        if m:
            stack.append(any(a in SLES_ATTRS for a in m.group(1).split(',')))
        elif line.startswith('endif::'):
            stack.pop()
        elif all(stack):
            pages += re.findall(r'xref:([\w.-]+)\.adoc', line)
    return pages


def candidate(site, docs):
    pages = nav_pages(docs)
    base = Path(site) / 'sles' / '15-SP7' / MODULE
    texts = []
    for p in pages:
        f = base / f'{p}.html'
        if not f.exists():
            texts.append(f'[[MISSING PAGE {p}]]')
            continue
        doc = html.parse(str(f)).getroot()
        cls = lambda c: f'contains(concat(" ", normalize-space(@class), " "), " {c} ")'
        art = doc.xpath(f'//article[{cls("doc")}]')[0]
        for bad in art.xpath(f'.//*[{cls("conum")}]/following-sibling::b[1]'
                             f' | .//*[{cls("conum")}] | .//td[{cls("icon")}]'
                             f' | .//*[{cls("colist")}]//td[1] | .//*[{cls("toc")}]'):
            bad.drop_tree()
        for img in art.xpath('.//img[@alt]'):
            img.tail = ' ' + img.get('alt') + ' ' + (img.tail or '')
        for t in art.xpath(f'.//*[{cls("title")}]'):
            if t.text:
                t.text = re.sub(r'^(Figure|Table|Example|Procedure) \d+\.\s*', '', t.text)
        for el in art.iter():
            if isinstance(el.tag, str) and el.tag in ('p', 'li', 'dt', 'dd', 'td', 'th', 'h1',
                                                      'h2', 'h3', 'h4', 'h5', 'pre', 'div',
                                                      'tr', 'table', 'ul', 'ol', 'dl', 'br'):
                el.tail = ' ' + (el.tail or '')
                el.text = ' ' + (el.text or '')
        texts.append(''.join(art.itertext()))
    return ' '.join(texts)


def norm(s):
    s = (s.replace(' ', ' ').replace('​', '').replace('“', '"').replace('”', '"')
          .replace('‘', "'").replace('’', "'").replace('…', '...').replace('›', ' '))
    s = re.sub(r'\s+([,.;:)!?])', r'\1', s)      # baseline spacing before punctuation
    s = s.replace('"', '')                           # DocBook generates <quote> marks
    return s.split()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True)
    ap.add_argument('--site', required=True)
    ap.add_argument('--docs', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--book', default='book_rmt.xml')
    ap.add_argument('--module', default='rmt')
    a = ap.parse_args()
    global MODULE
    MODULE = a.module
    b, c = norm(baseline(a.src, a.book)), norm(candidate(a.site, a.docs))
    sm = difflib.SequenceMatcher(None, b, c, autojunk=False)
    diffs = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != 'equal':
            diffs.append({'op': op,
                          'context': ' '.join(b[max(0, i1 - 6):i1]),
                          'docbook': ' '.join(b[i1:i2]),
                          'antora': ' '.join(c[j1:j2])})
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    (out / f'compare-diffs-{MODULE}.json').write_text(json.dumps(diffs, indent=1, ensure_ascii=False))
    lost = sum(len(d['docbook'].split()) for d in diffs)
    added = sum(len(d['antora'].split()) for d in diffs)
    from collections import Counter
    only_b, only_c = Counter(b) - Counter(c), Counter(c) - Counter(b)
    (out / f'compare-bag-{MODULE}.json').write_text(json.dumps(
        {'only_docbook': only_b.most_common(), 'only_antora': only_c.most_common()},
        indent=1, ensure_ascii=False))
    summary = {'docbook_words': len(b), 'antora_words': len(c),
               'bag_only_docbook': sum(only_b.values()), 'bag_only_antora': sum(only_c.values()),
               'similarity': round(sm.ratio(), 4), 'diff_hunks': len(diffs),
               'words_only_in_docbook': lost, 'words_only_in_antora': added}
    sd, sh = struct_db(a.src, a.book), struct_html(a.site, a.docs)
    summary['structure'] = {k: {'docbook': sd[k], 'antora': sh[k]} for k in sd}
    (out / f'compare-summary-{MODULE}.json').write_text(json.dumps(summary, indent=1))
    st = summary.pop('structure')
    print(f'{MODULE}: ' + json.dumps(summary))
    for k, v in st.items():
        flag = '' if v['docbook'] == v['antora'] else '   <-- MISMATCH'
        print(f"  {k:14} docbook={v['docbook']:5} antora={v['antora']:5}{flag}")
    summary['structure'] = st


if __name__ == '__main__':
    main()
