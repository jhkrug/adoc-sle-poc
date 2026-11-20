#!/usr/bin/env python3
"""Spike converter: one doc-sle DocBook book -> one Antora module.

Usage: db2adoc.py --src ../doc-sle --book book_rmt.xml --out docs
"""
import argparse
import gzip
import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from lxml import etree

from dbcommon import (DB, XI, XL, XMLID, M0, M1, PROFILE_ATTRS, SLES_PROFILE,
                      Entities, keep, ln, parse_marked)

PAGE_ROOTS = {'chapter', 'preface', 'appendix', 'glossary', 'article', 'book'}
SECT = {'sect1', 'sect2', 'sect3', 'sect4', 'sect5', 'section', 'simplesect'}
ADMON = {'note': 'NOTE', 'tip': 'TIP', 'warning': 'WARNING',
         'important': 'IMPORTANT', 'caution': 'CAUTION'}
MONO = set('command literal filename option package systemitem envar prompt tag '
           'parameter varname code function classname computeroutput userinput '
           'constant uri property'.split())
PLAIN_INLINE = set('phrase productname productnumber abbrev acronym firstterm '
                   'foreignphrase wordasword application orgname personname '
                   'citerefentry refentrytitle manvolnum'.split())
LISTS = {'itemizedlist', 'orderedlist', 'procedure', 'substeps', 'variablelist',
         'calloutlist', 'stepalternatives', 'simplelist', 'qandaset'}
SKIP = {'title', 'info', 'titleabbrev', 'subtitle', 'indexterm'}
KEYS = {'control': 'Ctrl', 'alt': 'Alt', 'shift': 'Shift', 'enter': 'Enter',
        'escape': 'Esc', 'tab': 'Tab', 'delete': 'Del', 'backspace': 'Backspace',
        'space': 'Space', 'meta': 'Meta'}
CO = '\ue002', '\ue003'   # callout markers inside screens


def q(tag):
    return f'{{{DB}}}{tag}'


def page_name(stem):
    return stem.replace('_', '-')


# --------------------------------------------------------------------------
# Escaping
# --------------------------------------------------------------------------
def esc_prose(s, code=False):
    s = s.replace('\\', '\x00BS\x00')
    s = re.sub(r'\{(?=[\w-]+\})', r'\\{', s)
    s = (s.replace('*', '{asterisk}').replace('`', '{backtick}')
          .replace('^', '{caret}').replace('~', '{tilde}').replace('#', '{hash}'))
    s = re.sub(r'(?<!\w)_|_(?!\w)', '{underscore}', s)
    if code:
        s = s.replace('+', '{plus}')        # `+ opens a passthrough
    else:
        s = re.sub(r'(?<!\s)\+|\+(?!\s)', '{plus}', s)
    s = s.replace('[[', '\\[[')
    if '<<' in s:
        s = s.replace('<<', '\\<<')
    s = re.sub(r'(?<=\w)--(?=\w)|(?<= )--(?= )', r'\\--', s)
    s = re.sub(r'(\.\.\.|->|=>|<-|<=|\(C\)|\(R\)|\(TM\))', r'\\\1', s)
    if code:
        s = re.sub(r"(?<=[A-Za-z0-9])'(?=[A-Za-z])", r"\\'", s)
    return s.replace('\x00BS\x00', '{backslash}')


def esc_screen(s, attrs, quotes):
    if attrs:
        s = re.sub(r'\{(?=[\w-]+\})', r'\\{', s)
    if quotes:
        s = s.replace('\\', '{backslash}')     # \ would escape the next __x__
        s = (s.replace('*', '{asterisk}').replace('`', '{backtick}')
              .replace('^', '{caret}').replace('~', '{tilde}')
              .replace('#', '{hash}').replace('_', '{underscore}'))
    s = re.sub(r'<(\d+)>(?=\s*$)', r'\\<\1>', s, flags=re.M)
    return s


def esc_macro_text(s):
    return s.replace(']', '\\]')


# --------------------------------------------------------------------------
# Whole-set index: which file is in which book/page, and all xml:ids
# --------------------------------------------------------------------------
class SetIndex:
    def __init__(self, xmldir, ents, main='MAIN.SLEDS.xml', profile=SLES_PROFILE):
        self.dir = Path(xmldir)
        self.trees, self.root_tag, self.parents = {}, {}, defaultdict(list)
        self.ids = {}
        for f in sorted(self.dir.glob('*.xml')):
            try:
                root = parse_marked(f, ents)
            except etree.XMLSyntaxError as e:
                print(f'WARN: cannot parse {f.name}: {e}')
                continue
            self.trees[f.name] = root
            self.root_tag[f.name] = ln(root)
            for inc in root.iter(f'{{{XI}}}include'):
                self.parents[inc.get('href')].append(f.name)
            for el in root.iter():
                i = el.get(XMLID) if isinstance(el.tag, str) else None
                if i and i not in self.ids:
                    self.ids[i] = (f.name, el)
        self._mod = {}
        self.reach = set()
        todo = [main]
        while todo:
            f = todo.pop()
            if f in self.reach or f not in self.trees:
                continue
            self.reach.add(f)
            for inc in self.trees[f].iter(f'{{{XI}}}include'):
                if all(keep(a, profile) for a in [inc, *inc.iterancestors()]):
                    todo.append(inc.get('href'))

    def ranked_parents(self, fn):
        """Parents of a file; files in the product's MAIN tree first."""
        ps = self.parents.get(fn, [])
        return [p for p in ps if p in self.reach] + [p for p in ps if p not in self.reach]

    def module_of(self, fn, seen=()):
        if fn in self._mod:
            return self._mod[fn]
        root = self.trees.get(fn)
        mod = None
        if root is not None and ln(root) in ('book', 'article') and root.get(XMLID):
            mod = re.sub(r'^(book|article|art)-', '', root.get(XMLID))
        else:
            for p in self.ranked_parents(fn):
                if p not in seen and not p.startswith('MAIN'):
                    mod = self.module_of(p, seen + (fn,))
                    if mod:
                        break
        self._mod[fn] = mod
        return mod

    def page_of(self, fn, seen=()):
        tag = self.root_tag.get(fn)
        if tag == 'book':
            return 'index'
        if tag in PAGE_ROOTS:
            return page_name(Path(fn).stem)
        for p in self.ranked_parents(fn):
            if p not in seen:
                return self.page_of(p, seen + (fn,))
        return page_name(Path(fn).stem)


# --------------------------------------------------------------------------
# Converter
# --------------------------------------------------------------------------
class Converter:
    def __init__(self, src, out, product='sles', version='15-SP7'):
        self.src = Path(src)
        self.xml = self.src / 'xml'
        self.out = Path(out)
        self.ents = Entities(self.xml)
        self.idx = SetIndex(self.xml, self.ents)
        self.product, self.version = product, version
        self.modules = []          # (module, book title) in build order
        self.ent_names = set()

    def set_book(self, book):
        self.book = book
        self.module = self.idx.module_of(book)
        self.stats = Counter()
        self.unknown = Counter()
        self.warn = []
        self.used_ents = Counter()
        self.ent_kinds = defaultdict(set)
        self.written = {}
        self.images = set()

    # ----- helpers -------------------------------------------------------
    def w(self, msg):
        self.warn.append(f'{self.cur_file}: {msg}')

    def cond(self, el):
        """List of ifdef expressions for an element's profiling attrs."""
        res = []
        for a in PROFILE_ATTRS:
            v = el.get(a)
            if v:
                pre = {'os': 'os', 'arch': 'arch', 'condition': 'cond'}[a]
                res.append(','.join(f'{pre}-{x}' for x in v.split(';') if x))
                self.stats[f'profiled-{a}'] += 1
        return res

    @staticmethod
    def wrap_cond(conds, text):
        for c in reversed(conds):
            text = f'ifdef::{c}[]\n{text}\n endif::[]'.replace('\n endif', '\nendif')
        return text

    def anchor(self, el):
        i = el.get(XMLID)
        return f'[#{i}]\n' if i else ''

    def title_of(self, el, mode='prose'):
        t = el.find(q('title'))
        if t is None:
            info = el.find(q('info'))
            if info is not None:
                t = info.find(q('title'))
        if t is None:
            return ''
        return self.one_line(self.inline(t, mode), 'title')

    def one_line(self, s, what):
        """Titles and terms must be one line: drop inline ifdef (logged)."""
        s = s.strip()
        if 'ifdef::' in s:
            self.w(f'{what} with inline profiling flattened: {s!r}')
            self.stats[f'{what}-profiling-flattened'] += 1
            s = re.sub(r'\n?ifdef::[^\[]+\[(.*?)\]\n?', r'\1', s)
        return re.sub(r'\s+', ' ', s)

    def abstract_of(self, el, level):
        info = el.find(q('info'))
        ab = info.find(q('abstract')) if info is not None else None
        if ab is None:
            return ''
        body = '\n\n'.join(self.blocks(ab, level))
        return f'[abstract]\n--\n{body}\n--' if body.strip() else ''

    def description_of(self, el):
        info = el.find(q('info'))
        if info is None:
            return ''
        for m in info.findall(q('meta')):
            if m.get('name') == 'description':
                return re.sub(r'\s+', ' ', self.inline(m, 'prose')).strip()
        return ''

    # ----- inline --------------------------------------------------------
    def text(self, s, mode):
        if not s:
            return ''
        parts = re.split(f'{M0}([^{M1}]+){M1}', s)
        out = []
        for i, p in enumerate(parts):
            if i % 2:
                out.append(self.entity(p, mode))
            elif mode == 'screen':
                out.append(esc_screen(p, self.scr_attrs, self.scr_quotes))
            elif mode == 'plain':
                out.append(re.sub(r'\s+', ' ', p))
            else:
                out.append(esc_prose(re.sub(r'\s+', ' ', p), code=(mode == 'code')))
        return ''.join(out)

    def entity(self, name, mode):
        k = self.ents.kind(name)
        self.used_ents[name] += 1
        self.ent_kinds[k].add(name)
        if k in ('attr', 'variant'):
            return '{' + self.attr_name(name) + '}'
        if k == 'inline':
            return self.inline(self.ents.fragment(name), mode)
        if k == 'block':
            self.w(f'block entity &{name}; used inline; rendered as text')
            return self.ents.plain(name, SLES_PROFILE)
        self.w(f'unknown entity &{name};')
        return '{' + self.attr_name(name) + '}'

    def attr_name(self, name):
        """AsciiDoc attribute names are case-insensitive; entity names are not."""
        base = name.lower().replace('.', '-')
        if name != name.lower() and name.lower() in self.ents.raw:
            return base + '-cap'            # &AS; next to &as;
        return base

    def inline(self, el, mode):
        if mode != 'screen':
            return fix_apos(self._inline(el, mode))
        return self._inline(el, mode)

    def _inline(self, el, mode):
        out = [self.text(el.text, mode)]
        for c in el:
            r = self.inline_el(c, mode)
            if isinstance(r, tuple):              # (mark, inner) formatting pair
                mark, inner = r
                prev = ''.join(out)[-1:]
                tail = (c.tail or '')[:1]
                r = self.fmt(mark, inner, prev, tail)
            out.append(r)
            out.append(self.text(c.tail, mode))
        return ''.join(out)

    @staticmethod
    def fmt(mark, inner, prev, nxt):
        if not inner.strip():
            return inner
        lead = inner[:len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        core = inner.strip()
        # Asciidoctor: a constrained span cannot open after a word char or
        # after ; : " ' ` }  (and '}' ends {tilde}, {apos}, {underscore} ...)
        unc = bool(re.match(r'\w', prev or '') or re.match(r'\w', nxt or '')) \
            or (prev or ' ') in ';:"\'`}' \
            or core.startswith(mark) or core.endswith(mark)
        # two formatted spans side by side (`a``b`) would merge. A constrained
        # span cannot open after '}', so use {empty} + an unconstrained span.
        sep = ''
        if not lead and prev and prev in '`*_#^~':
            sep, unc = '{empty}', True
        elif not lead and prev == ']':      # [x]`y` would be an attribute list
            sep = '{empty}'
        m = mark * 2 if unc else mark
        return f'{sep}{lead}{m}{core}{m}{trail}'

    def inline_el(self, c, mode):
        tag = ln(c)
        if not tag:
            return ''
        conds = self.cond(c)
        r = self._inline_el(c, tag, mode)
        if conds:
            if isinstance(r, tuple):
                r = self.fmt(r[0], r[1], '', '')
            if len(conds) == 1 and '\n' not in r:
                r = f'\nifdef::{conds[0]}[{r}]\n'
            else:
                r = '\n' + self.wrap_cond(conds, r) + '\n'
            self.stats['inline-profiling'] += 1
        return r

    def _inline_el(self, c, tag, mode):
        if mode == 'screen':
            if tag == 'co':
                return self.callout_marker(c)
            if tag in ('replaceable', 'emphasis'):
                inner = self.inline(c, mode)
                m = '__' if tag == 'replaceable' else '**'
                core = inner.strip()
                if not core:
                    return inner
                return inner[:len(inner) - len(inner.lstrip())] + m + core + m + \
                    inner[len(inner.rstrip()):]
            if tag in ('remark', 'indexterm'):
                return ''
            return self.inline(c, mode)
        if tag in MONO:
            return self.code(c, mode)
        if tag == 'replaceable':
            return ('_', self.inline(c, mode))
        if tag == 'emphasis':
            bold = c.get('role') in ('bold', 'strong')
            return ('*' if bold else '_', self.inline(c, mode))
        if tag in ('guimenu', 'guilabel', 'guibutton', 'guisubmenu', 'guimenuitem', 'guiicon'):
            return ('*', self.inline(c, mode))
        if tag == 'menuchoice':
            items = [re.sub(r'\s+', ' ', self.inline(g, 'prose')).strip()
                     for g in c if ln(g).startswith('gui')]
            self.stats['menuchoice'] += 1
            if len(items) == 1:
                return ('*', items[0])
            if any('>' in i for i in items):      # '>' is the menu macro separator
                self.stats['menuchoice-fallback'] += 1
                return ' \u203a '.join(f'*{i}*' for i in items)
            return f'menu:{esc_macro_text(items[0])}[{" > ".join(esc_macro_text(i) for i in items[1:])}]'
        if tag == 'keycap':
            return f'kbd:[{esc_macro_text(self.key(c))}]'
        if tag == 'keycombo':
            keys = [self.key(k) for k in c if ln(k) == 'keycap']
            sep = ',' if any('+' in k for k in keys) else '+'   # kbd: allows ',' too
            # a literal '+' can pair with another '+' into a passthrough: use {plus}
            keys = [k.replace('+', '{plus}') for k in keys]
            return f'kbd:[{esc_macro_text(sep.join(keys))}]'
        if tag == 'xref':
            return self.xref(c)
        if tag == 'link':
            return self.link(c)
        if tag == 'email':
            addr = self.plaintext(c)
            return f'mailto:{addr}[]'
        if tag == 'quote':
            return f'\u201c{self.inline(c, mode)}\u201d'
        if tag == 'superscript':
            return f'^{self.inline(c, mode).strip()}^'
        if tag == 'subscript':
            return f'~{self.inline(c, mode).strip()}~'
        if tag == 'citetitle':
            return ('_', self.inline(c, mode))
        if tag == 'trademark':
            sym = {'registered': '®', 'copyright': '©', 'service': '℠'}.get(c.get('class'), '™')
            return self.inline(c, mode) + sym
        if tag in PLAIN_INLINE:
            return self.inline(c, mode)
        if tag == 'inlinemediaobject':
            return self.image(c, inline=True)
        if tag == 'footnote':
            body = ' '.join(self.inline(p, 'prose').strip() for p in c)
            return f'footnote:[{esc_macro_text(body)}]'
        if tag == 'anchor':
            return f'[[{c.get(XMLID)}]]'
        if tag in ('remark', 'indexterm', 'co'):
            self.stats[f'dropped-inline-{tag}'] += 1
            return ''
        self.unknown[f'inline:{tag}'] += 1
        return self.inline(c, mode)

    def plaintext(self, el):
        """Text of an element with entity markers resolved for the profile."""
        t = re.sub(r'\s+', ' ', ''.join(el.itertext())).strip()
        return re.sub(f'{M0}([^{M1}]+){M1}',
                      lambda m: self.ents.plain(m.group(1), SLES_PROFILE)
                      if m.group(1) in self.ents.raw else m.group(1), t)

    def key(self, k):
        t = self.plaintext(k)
        return t or KEYS.get(k.get('function', ''), k.get('function', '?'))

    def code(self, c, mode):
        has_children = any(isinstance(x.tag, str) for x in c)
        raw = ''.join(c.itertext())
        if mode == 'code':      # nested mono: just the content
            return self.inline(c, 'code')
        if not has_children and M0 not in raw and '+' not in raw and '`' not in raw \
                and raw.strip():
            core = re.sub(r'\s+', ' ', raw).strip()
            return ('`', f'+{core}+')
        return ('`', self.inline(c, 'code'))

    def xref(self, c):
        target = c.get('linkend')
        self.stats['xref'] += 1
        hit = self.idx.ids.get(target)
        if not hit:
            self.stats['xref-unresolved'] += 1
            self.w(f'xref to unknown id {target}')
            return f'<<{target}>>'
        fn, el = hit
        tmod, tpage = self.idx.module_of(fn), self.idx.page_of(fn)
        is_root = el is self.idx.trees[fn] and self.idx.root_tag[fn] in PAGE_ROOTS
        title = self.title_of(el)
        if not title:
            if ln(el) == 'varlistentry':
                t = el.find(q('term'))
                title = self.inline(t, 'prose').strip() if t is not None else ''
            if not title:
                title = {'step': 'this step', 'listitem': 'this item'}.get(ln(el), target)
                self.stats['xref-generated-text'] += 1
        title = esc_macro_text(re.sub(r'\s+', ' ', title))
        if tmod == self.module and tpage == self.cur_page:
            self.stats['xref-same-page'] += 1
            return f'<<{target},{title}>>'
        prefix = '' if tmod == self.module else f'{tmod}:'
        if tmod != self.module:
            self.stats['xref-other-book'] += 1
        else:
            self.stats['xref-other-page'] += 1
        if is_root:
            return f'xref:{prefix}{tpage}.adoc[]'
        return f'xref:{prefix}{tpage}.adoc#{target}[{title}]'

    def link(self, c):
        href = c.get(f'{{{XL}}}href', '')
        href = re.sub(f'{M0}([^{M1}]+){M1}',
                      lambda m: self.ents.plain(m.group(1), SLES_PROFILE)
                      if self.ents.kind(m.group(1)) == 'inline'
                      else '{' + self.attr_name(m.group(1)) + '}', href)
        text = re.sub(r'\s+', ' ', self.inline(c, 'prose')).strip()
        self.stats['link'] += 1
        return f'link:{href}[{esc_macro_text(text)}]'

    def callout_marker(self, c):
        self.co_num += 1
        if c.get(XMLID):
            self.co_map[c.get(XMLID)] = self.co_num
        return f'{CO[0]}{self.co_num}{CO[1]}'

    # ----- blocks --------------------------------------------------------
    def blocks(self, el, level):
        res, buf = [], []

        def flush():
            s = re.sub(r'[ \t]+', ' ', ''.join(buf)).strip()
            s = fix_apos(re.sub(r' *\n[\s]*\n *', '\n', s))
            lines = s.split('\n')
            for k in range(1, len(lines)):
                if re.match(r'[.*\-=|/:\[+<]', lines[k]) and not lines[k].startswith(
                        ('ifdef::', 'endif::')):
                    lines[k] = '{empty}' + lines[k]
                    self.stats['line-start-guard'] += 1
            s = '\n'.join(lines)
            if s:
                res.append(s)
            buf.clear()
        def add_text(t):
            # a block entity (for example a <note>) in text between blocks
            for i, part in enumerate(re.split(f'({M0}[^{M1}]+{M1})', t)):
                n = part[1:-1] if i % 2 else None
                if n and self.ents.kind(n) == 'block':
                    flush()
                    self.stats['block-entity-expanded'] += 1
                    self.used_ents[n] += 1
                    res.extend(self.blocks(self.ents.fragment(n), level))
                elif part:
                    buf.append(self.text(part, 'prose'))
        if el.text and el.text.strip():
            add_text(el.text)
        for c in el:
            tag = ln(c)
            if tag and tag not in SKIP and self.is_block(c) and not (
                    tag == 'remark' and ln(el) in ('para', 'simpara')):
                flush()
                b = self.block(c, level)
                if b:
                    res.append(b)
            elif tag in SKIP:
                pass
            elif tag:
                r = self.inline_el(c, 'prose')
                if isinstance(r, tuple):
                    prev = ''.join(buf)[-1:]
                    r = self.fmt(r[0], r[1], prev, (c.tail or '')[:1])
                buf.append(r)
            # inside an inline run a whitespace tail is a word space: keep it
            if c.tail and (c.tail.strip() or buf):
                add_text(c.tail)
        flush()
        return res

    def is_block(self, c):
        return ln(c) in BLOCKS or ln(c) == 'include'

    def para(self, el, level):
        """A para may contain block children (lists, screens)."""
        parts = self.blocks(el, level)
        return '\n\n'.join(fix_lead(p) for p in parts)

    def block(self, c, level):
        tag = ln(c)
        if not tag:
            return ''
        conds = self.cond(c)
        out = self._block(c, tag, level)
        if out and conds:
            out = self.wrap_cond(conds, out)
        return out

    def _block(self, c, tag, level):
        self.stats[f'block-{tag}'] += 1
        if tag in SECT:
            return self.section(c, level)
        if tag in ('para', 'simpara'):
            return self.para(c, level)
        if tag == 'formalpara':
            # the body can be a <para> or a block entity (&kernel_cpu_mitigations_auto;)
            body = '\n\n'.join(fix_lead(b) for b in self.blocks(c, level))
            if not body.strip():            # title only: a .Title needs a block
                return f'{self.anchor(c)}*{self.title_of(c)}*'
            return f'{self.anchor(c)}.{self.title_of(c)}\n{body}'
        if tag in LISTS:
            return self.list_block(c, level)
        if tag in ('screen', 'programlisting', 'synopsis', 'literallayout'):
            return self.screen(c)
        if tag in ADMON:
            body = '\n\n'.join(self.blocks(c, level))
            d = '=' * (4 + 2 * self.delim_depth)
            t = self.title_of(c)
            return (f'{self.anchor(c)}[{ADMON[tag]}]\n' + (f'.{t}\n' if t else '') +
                    f'{d}\n{body}\n{d}')
        if tag == 'example':
            self.delim_depth += 1
            body = '\n\n'.join(self.blocks(c, level))
            self.delim_depth -= 1
            d = '=' * (4 + 2 * self.delim_depth)
            return f'{self.anchor(c)}.{self.title_of(c)}\n{d}\n{body}\n{d}'
        if tag in ('figure', 'informalfigure'):
            t = self.title_of(c)
            mo = c.find(q('mediaobject'))
            img = self.image(mo if mo is not None else c)
            return self.anchor(c) + (f'.{t}\n' if t else '') + img
        if tag == 'mediaobject':
            return self.anchor(c) + self.image(c)
        if tag in ('table', 'informaltable'):
            return self.table(c)
        if tag == 'bridgehead':
            return (f'{self.anchor(c)}[discrete]\n' + '=' * (level + 2) + ' ' +
                    re.sub(r'\s+', ' ', self.inline(c, 'prose')).strip())
        if tag == 'remark':
            self.stats['remark'] += 1
            return f'// REMARK: {self.plaintext(c)}'
        if tag == 'include':
            return self.include(c, level)
        if tag == 'blockquote':
            return f'[quote]\n____\n' + '\n\n'.join(self.blocks(c, level)) + '\n____'
        if tag == 'sidebar':
            return f'.{self.title_of(c)}\n****\n' + '\n\n'.join(self.blocks(c, level)) + '\n****'
        if tag in ('legalnotice', 'abstract'):
            t = self.title_of(c)
            return (f'[discrete]\n== {t}\n\n' if t else '') + '\n\n'.join(self.blocks(c, level))
        self.unknown[f'block:{tag}'] += 1
        return '\n\n'.join(self.blocks(c, level))

    def section(self, c, level):
        depth = level + 1
        if depth > 5:
            self.w(f'section depth {depth} > 5')
        head = f'{self.anchor(c)}{"=" * (depth + 1)} {self.title_of(c)}'
        parts = [head]
        ab = self.abstract_of(c, depth)
        if ab:
            parts.append(ab)
        parts += self.blocks(c, depth)
        return '\n\n'.join(parts)

    # lists
    def list_block(self, c, level):
        tag = ln(c)
        title = self.title_of(c)
        head = self.anchor(c) + (f'.{title}\n' if title else '')
        if tag == 'variablelist':
            self.dl_depth += 1
            items = [self.vlentry(e, level) for e in c if ln(e) == 'varlistentry']
            self.dl_depth -= 1
        elif tag == 'qandaset':
            head += '[qanda]\n'
            self.dl_depth += 1
            items = [self.qanda(e, level) for e in c.iter(q('qandaentry'))]
            self.dl_depth -= 1
        elif tag == 'calloutlist':
            items = [self.callout_item(e, level) for e in c if ln(e) == 'callout']
        else:
            ordered = tag in ('orderedlist', 'procedure', 'substeps')
            key = '.' if ordered else '*'
            self.depth[key] += 1
            marker = key * self.depth[key]
            kids = [e for e in c if ln(e) in ('listitem', 'step', 'member')]
            if tag == 'simplelist':
                head += '[none]\n'
            # procedures: DocBook renders single-step procedures unnumbered too,
            # keep them as ordered for simplicity.
            items = [self.list_item(e, marker, level) for e in kids]
            self.depth[key] -= 1
            pre = [self.block(e, level) for e in c
                   if ln(e) not in ('listitem', 'step', 'member', 'title', 'info') and ln(e)]
            if pre:
                head += '\n\n'.join(p for p in pre if p) + '\n\n'
        return head + '\n'.join(i for i in items if i)

    def item_body(self, e, level):
        """Principal text + attached blocks for a list item."""
        conds = self.cond(e)
        anchor = f'[[{e.get(XMLID)}]]' if e.get(XMLID) else ''
        kids = self.blocks(e, level)
        # mark which child blocks are lists so we can protect following blocks
        first = ''
        if kids and not kids[0].startswith(('[', '.', '----', '|===', 'ifdef::', '//', '====',
                                            '*', '|')) \
                and '\n\n' not in kids[0] and not re.match(r'^(\.+|\*+) ', kids[0]):
            first = kids.pop(0)
        first = anchor + (first if first else '{empty}')
        rest = []
        for i, b in enumerate(kids):
            is_list = bool(re.match(r'^(\[#[^\]]+\]\n)?(\.[^\n]*\n)?(\.+|\*+) |^[^\n]*::', b))
            if is_list and i < len(kids) - 1:
                if self.in_open:
                    self.stats['nested-open-block-needed'] += 1
                    self.w('nested list followed by blocks inside open block')
                else:
                    b = f'--\n{b}\n--'
                    self.stats['open-block-wrap'] += 1
            rest.append(b)
        body = first + ''.join(f'\n+\n{b}' for b in rest)
        return body, conds

    def list_item(self, e, marker, level):
        body, conds = self.item_body(e, level)
        s = f'{marker} {body}'
        return self.wrap_cond(conds, s) if conds else s

    def vlentry(self, e, level):
        terms = [self.one_line(self.inline(t, 'prose'), 'term')
                 for t in e.findall(q('term'))]
        if e.get(XMLID):
            terms[0] = f'[[{e.get(XMLID)}]]' + terms[0]
        li = e.find(q('listitem'))
        body, _ = self.item_body(li, level) if li is not None else ('{empty}', [])
        sep = '::' if self.dl_depth == 1 else ':' * (self.dl_depth + 1)
        term_line = ', '.join(terms).replace('\n', ' ') or '{empty}'
        conds = self.cond(e)
        s = f'{term_line}{sep}\n{body}'
        return self.wrap_cond(conds, s) if conds else s

    def qanda(self, e, level):
        qu, an = e.find(q('question')), e.find(q('answer'))
        qparts = self.blocks(qu, level) if qu is not None else []
        qtext = ' '.join(p.replace('\n', ' ') for p in qparts) or '{empty}'
        ids = [x.get(XMLID) for x in (e, qu) if x is not None and x.get(XMLID)]
        body, _ = self.item_body(an, level) if an is not None else ('{empty}', [])
        s = ''.join(f'[[{i}]]' for i in ids) + f'{qtext}::\n{body}'
        conds = self.cond(e)
        return self.wrap_cond(conds, s) if conds else s

    def callout_item(self, e, level):
        nums = [self.co_map.get(r, '?') for r in (e.get('arearefs') or '').split()]
        body, _ = self.item_body(e, level)
        return f'<{nums[0] if nums else "?"}> {body}'

    # screens
    def screen(self, c):
        raw = etree.tostring(c, encoding='unicode')
        self.co_num = 0            # Asciidoctor numbers callouts per listing block
        self.scr_quotes = c.find('.//' + q('replaceable')) is not None or \
            c.find('.//' + q('emphasis')) is not None
        attr_ents = [n for n in re.findall(f'{M0}([^{M1}]+){M1}', raw)
                     if self.ents.kind(n) in ('attr', 'variant', 'unknown')]
        self.scr_attrs = bool(attr_ents) or self.scr_quotes
        body = self.inline(c, 'screen')
        quotes, attr_sub = self.scr_quotes, self.scr_attrs
        self.scr_attrs = self.scr_quotes = False
        # move callout markers to end of their line
        lines = []
        for line in body.split('\n'):
            nums = re.findall(f'{CO[0]}(\\d+){CO[1]}', line)
            line = re.sub(f'{CO[0]}\\d+{CO[1]}', '', line)
            if nums:
                line = line.rstrip() + ' ' + ' '.join(f'<{n}>' for n in nums)
                self.stats['callouts'] += len(nums)
            lines.append(line)
        body = '\n'.join(lines).strip('\n')
        if quotes:                          # ]__x__ would be an attribute list
            body = re.sub(r'\](?=__|\*\*)', ']{empty}', body)
        attrs = []
        lang = c.get('language')
        if lang:
            attrs.append(f'source,{lang}')
        if quotes:
            attrs.append('subs="specialchars,quotes,attributes,callouts"')
            self.stats['screen-quotes'] += 1
        elif attr_sub:
            attrs.append('subs="attributes+"')
            self.stats['screen-attributes'] += 1
        head = self.anchor(c) + (f'[{",".join(attrs)}]\n' if attrs else '')
        if body.count('\n----') or body.startswith('----'):
            delim = '-' * 6
        else:
            delim = '----'
        return f'{head}{delim}\n{body}\n{delim}'

    # images
    def image(self, el, inline=False):
        objs = el.findall('.//' + q('imageobject'))
        pick = None
        for o in objs:
            if o.get('role') in ('html', None):
                pick = o
                break
        pick = pick if pick is not None else (objs[0] if objs else None)
        if pick is None:
            self.w('mediaobject without imageobject')
            return ''
        data = pick.find(q('imagedata'))
        fref = data.get('fileref')
        found = self.find_image(fref)
        tobjs = el.findall('.//' + q('textobject'))
        desc = [t for t in tobjs if t.get('role') == 'description']
        if desc and len(tobjs) > 1:
            self.stats['image-two-textobjects-description-used'] += 1
        alt_el = desc[0] if desc else (tobjs[0] if tobjs else None)
        alt = self.plaintext(alt_el) if alt_el is not None else ''
        alt = esc_macro_text(alt.replace('"', "'"))
        name = found.name if found else fref
        self.images.add(found) if found else self.w(f'image not found: {fref}')
        width = data.get('width', '')
        opts = f'"{alt}"' + (f',width={width}' if width.endswith('%') else '')
        return f'image:{name}[{opts}]' if inline else f'image::{name}[{opts}]'

    def find_image(self, fref):
        base = self.src / 'images' / 'src'
        stem = Path(fref).stem
        for sub, ext in (('svg', '.svg'), ('png', '.png'), ('jpg', '.jpg')):
            p = base / sub / (stem + ext)
            if Path(fref).suffix == ext and p.exists():
                return p
        for sub in ('png', 'svg', 'jpg'):
            for p in (base / sub).glob(stem + '.*'):
                return p
        dia = base / 'dia' / (stem + '.dia')
        if dia.exists():           # DAPS converts .dia at build time; do it here
            cache = Path('/tmp/adoc-sle-poc-dia')
            cache.mkdir(exist_ok=True)
            out = cache / (stem + '.svg')
            if not out.exists():
                plain = cache / (stem + '.dia')
                data = dia.read_bytes()
                plain.write_bytes(gzip.decompress(data) if data[:2] == b'\x1f\x8b' else data)
                subprocess.run(['dia', '-n', '-e', str(out), '-t', 'svg', str(plain)],
                               capture_output=True, timeout=120)
            if out.exists():
                self.stats['image-dia-converted'] += 1
                return out
        return None

    # tables
    def table(self, c):
        tg = c.find(q('tgroup'))
        if tg is None:
            self.w('HTML table not supported in spike')
            return ''
        ncols = int(tg.get('cols', '1'))
        specs = tg.findall(q('colspec'))
        names = [s.get('colname') for s in specs]
        widths = []
        for s in specs:
            m = re.match(r'([\d.]+)\*', s.get('colwidth', ''))
            widths.append(m.group(1) if m else '1')
        if len(widths) != ncols:
            widths = ['1'] * ncols
        rows = []
        thead = tg.find(q('thead'))
        for part in (thead, tg.find(q('tbody'))):
            if part is None:
                continue
            for row in part.findall(q('row')):
                cells = []
                for en in row.findall(q('entry')):
                    cs, rs = 1, int(en.get('morerows', '0')) + 1
                    if en.get('namest') and en.get('nameend') and names:
                        cs = names.index(en.get('nameend')) - names.index(en.get('namest')) + 1
                    span = (str(cs) if cs > 1 else '') + (f'.{rs}' if rs > 1 else '')
                    span += '+' if span else ''
                    blk = any(self.is_block(x) for x in en)
                    if blk:
                        content = '\n\n'.join(self.blocks(en, 0))
                        cells.append(f'{span}a|{content}')
                    else:
                        content = re.sub(r'[ \t]+', ' ', self.inline(en, 'prose')).strip()
                        content = re.sub(r' *\n\s*', '\n', content)
                        cells.append(f'{span}|{content.replace("|", "{vbar}")}')
                rows.append('\n'.join(cells))
            if part is thead:
                rows.append('')
        t = self.title_of(c)
        opts = ',options="header"' if thead is not None else ''
        head = self.anchor(c) + (f'.{t}\n' if t else '')
        return f'{head}[cols="{",".join(widths)}"{opts}]\n|===\n' + \
            '\n'.join(rows).replace('\n\n\n', '\n\n') + '\n|==='

    # includes
    def include(self, inc, level):
        href = inc.get('href')
        tag = self.idx.root_tag.get(href)
        if tag is None:
            self.w(f'include of unknown file {href}')
            return ''
        if tag in PAGE_ROOTS:
            self.w(f'page-level include {href} inside a page; skipped')
            return ''
        if tag == 'legalnotice':
            return self.convert_partial(href, level, inline_only=True)
        target = self.convert_partial(href, level)
        root_level = 0 if tag in ('sect1', 'section') else level
        off = level - root_level
        return f'include::{target}[' + (f'leveloffset=+{off}' if off else '') + ']'

    # ----- files ---------------------------------------------------------
    def revdate(self, fn):
        r = subprocess.run(['git', '-C', str(self.src), 'log', '-1', '--format=%as', '--',
                            f'xml/{fn}'], capture_output=True, text=True)
        return r.stdout.strip()

    def reset_state(self, fn, page):
        self.cur_file, self.cur_page = fn, page
        self.depth = Counter()
        self.dl_depth = 0
        self.delim_depth = 0
        self.in_open = False
        self.co_num = 0
        self.co_map = {}
        self.scr_attrs = self.scr_quotes = False

    def partial_target(self, fn):
        shared = fn.startswith('common_')
        mod = 'ROOT' if shared else self.module
        path = self.out / 'modules' / mod / 'partials' / (page_name(Path(fn).stem) + '.adoc')
        ref = f'{"ROOT:" if shared else ""}partial${path.name}'
        return path, ref

    def convert_partial(self, fn, level, inline_only=False):
        root = self.idx.trees[fn]
        saved = (self.cur_file, self.cur_page, self.depth, self.dl_depth,
                 self.delim_depth, self.co_num, self.co_map)
        self.cur_file = fn
        self.depth, self.dl_depth, self.delim_depth = Counter(), 0, 0
        self.co_num, self.co_map = 0, {}
        if inline_only:
            body = self._block(root, ln(root), level)
        else:
            body = self.block(root, 0 if ln(root) in ('sect1', 'section') else level)
        (self.cur_file, self.cur_page, self.depth, self.dl_depth,
         self.delim_depth, self.co_num, self.co_map) = saved
        if inline_only:
            return body
        path, ref = self.partial_target(fn)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'// Converted from xml/{fn}\n\n{body}\n')
        self.written[fn] = str(path.relative_to(self.out))
        return ref

    def convert_page(self, fn, conds=None):
        root = self.idx.trees[fn]
        page = page_name(Path(fn).stem)
        self.reset_state(fn, page)
        title = self.title_of(root)
        head = [f'// Converted from xml/{fn}']
        if root.get(XMLID):
            head.append(f'[#{root.get(XMLID)}]')
        head.append(f'= {title}')
        desc = self.description_of(root)
        if desc:
            head.append(f':description: {desc}')
        rd = self.revdate(fn)
        if rd:
            head.append(f':revdate: {rd}')
        parts = ['\n'.join(head)]
        ab = self.abstract_of(root, 0)
        if ab:
            parts.append(ab)
        parts += self.blocks(root, 0)
        text = '\n\n'.join(parts) + '\n'
        path = self.out / 'modules' / self.module / 'pages' / f'{page}.adoc'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.written[fn] = str(path.relative_to(self.out))
        return page

    def convert_book(self):
        root = self.idx.trees[self.book]
        self.reset_state(self.book, 'index')
        info = root.find(q('info'))
        title = self.title_of(root)
        nav = [f'* xref:index.adoc[]']
        idx = [f'// Converted from xml/{self.book}', f'[#{root.get(XMLID)}]', f'= {title}']
        desc = self.description_of(root)
        if desc:
            idx.append(f':description: {desc}')
        idx.append(f':revdate: {self.revdate(self.book)}')
        body = []
        ab = self.abstract_of(root, 0)
        if ab:
            body.append(ab)
        if info is not None:
            for inc in info.iter(f'{{{XI}}}include'):
                body.append(self.include(inc, 0))
        def walk(parent, depth):
            for el in parent:
                tag = ln(el)
                if tag == 'include':
                    href = el.get('href')
                    if self.idx.root_tag.get(href) in PAGE_ROOTS:
                        page = self.convert_page(href)
                        line = f'{"*" * depth} xref:{page}.adoc[]'
                        conds = self.cond(el)
                        nav.append(self.wrap_cond(conds, line) if conds else line)
                elif tag == 'part':
                    # no partintro in these books: a part is a nav group only
                    self.reset_state(self.book, 'index')
                    if el.find(q('partintro')) is not None:
                        self.w('partintro dropped')
                    line = f'{"*" * depth} {self.title_of(el)}'
                    conds = self.cond(el)
                    nav.append(self.wrap_cond(conds, line) if conds else line)
                    self.stats['part'] += 1
                    walk(el, depth + 1)
        walk(root, 2)
        self.reset_state(self.book, 'index')
        (self.out / 'modules' / self.module / 'pages' / 'index.adoc').write_text(
            '\n'.join(idx) + '\n\n' + '\n\n'.join(b for b in body if b) + '\n')
        (self.out / 'modules' / self.module / 'nav.adoc').write_text('\n'.join(nav) + '\n')
        self.modules.append((self.module, title))

    def write_component(self):
        attrs = {
            'experimental': '',
            'hash': '#', 'underscore': '_',
            'os-sles': '',
            **{f'arch-{a}': '' for a in sorted(SLES_PROFILE['arch'])},
            **{f'cond-{c}': '' for c in sorted(SLES_PROFILE['condition'])},
        }
        names = {}
        for n in sorted(self.ents.raw):
            k = self.ents.kind(n)
            if k in ('attr', 'variant'):
                an = self.attr_name(n)
                if an in names and names[an] != n:
                    self.w(f'attribute name collision: {n} vs {names[an]}')
                    continue
                names[an] = n
                attrs[an] = self.ents.plain(n, SLES_PROFILE)
        root_pages = self.out / 'modules' / 'ROOT' / 'pages'
        root_pages.mkdir(parents=True, exist_ok=True)
        (root_pages / 'index.adoc').write_text(
            '= {productname} {productnumber} documentation\n\n' +
            'Converted from DocBook by the doc-sle Antora spike.\n\n' +
            ''.join(f'* xref:{m}:index.adoc[]\n' for m, _ in self.modules))
        (self.out / 'modules' / 'ROOT' / 'nav.adoc').write_text('* xref:index.adoc[]\n')
        lines = ['name: sles', f"version: '{self.version}'",
                 'title: SUSE Linux Enterprise Server', 'start_page: ROOT:index.adoc',
                 'nav:', '- modules/ROOT/nav.adoc'] + \
            [f'- modules/{m}/nav.adoc' for m, _ in self.modules] + ['asciidoc:', '  attributes:']
        for k, v in attrs.items():
            lines.append(f'    {k}: {json.dumps(v, ensure_ascii=False)}')
        (self.out / 'antora.yml').write_text('\n'.join(lines) + '\n')
        coll = [w for w in self.warn if 'collision' in w]
        print(f'antora.yml: {len(attrs)} attributes, {len(coll)} name collisions')
        for w in coll:
            print('  ', w)

    def copy_images(self):
        d = self.out / 'modules' / self.module / 'images'
        d.mkdir(parents=True, exist_ok=True)
        for p in self.images:
            shutil.copy2(p, d / p.name)
        self.images = set()

    def report(self):
        rep = {
            'module': self.module,
            'files': self.written,
            'stats': dict(sorted(self.stats.items())),
            'unknown_elements': dict(self.unknown.most_common()),
            'entity_kinds_used': {k: sorted(v) for k, v in self.ent_kinds.items()},
            'warnings': self.warn,
        }
        (self.out.parent / 'reports').mkdir(exist_ok=True)
        (self.out.parent / 'reports' / f'convert-report-{self.module}.json').write_text(
            json.dumps(rep, indent=1, ensure_ascii=False))
        self.warn = [w for w in self.warn]
        print(f'{self.module}: {len(self.written)} files, unknown elements: '
              f'{rep["unknown_elements"] or "none"}, {len(self.warn)} warnings')


def etree_wrap(c):
    """Wrap a single inline element so Converter.inline renders it."""
    w = etree.Element(q('phrase'))
    tail = c.tail
    w.append(_copy(c))
    w[0].tail = None
    c.tail = tail
    return w


def _copy(c):
    import copy
    return copy.deepcopy(c)


def fix_apos(s):
    """An apostrophe next to a backtick is AsciiDoc smart-quote syntax."""
    return s.replace("`'", "`{apos}").replace("'`", "{apos}`")


def fix_lead(s):
    """Protect paragraphs that start with a newline or a block-like marker."""
    if s.startswith('\n'):
        s = '{empty}' + s
    if re.match(r'^(\.+|\*+|-|=+|\||//|:\w+:) ', s):
        s = '{empty}' + s
    return s


BLOCKS = (SECT | LISTS | set(ADMON) | {
    'para', 'simpara', 'formalpara', 'screen', 'programlisting', 'synopsis',
    'literallayout', 'example', 'figure', 'informalfigure', 'mediaobject', 'table',
    'informaltable', 'bridgehead', 'remark', 'blockquote', 'sidebar', 'legalnotice',
    'abstract', 'variablelist'})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True)
    ap.add_argument('--book', action='append', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    out = Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    c = Converter(a.src, out)
    for book in a.book:
        c.set_book(book)
        c.images = set()
        c.convert_book()
        c.copy_images()
        c.report()
    c.write_component()


if __name__ == '__main__':
    main()
