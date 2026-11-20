"""Shared helpers for the doc-sle DocBook -> AsciiDoc spike.

Entity loading, entity classification, entity marking and profiling.
"""
import re
from pathlib import Path
from lxml import etree

DB = 'http://docbook.org/ns/docbook'
XI = 'http://www.w3.org/2001/XInclude'
XL = 'http://www.w3.org/1999/xlink'
XMLID = '{http://www.w3.org/XML/1998/namespace}id'
M0, M1 = '', ''          # entity marker delimiters (private use chars)
BUILTIN = {'amp', 'lt', 'gt', 'quot', 'apos'}
PROFILE_ATTRS = ('os', 'arch', 'condition')
BLOCK_TAGS = r'para|screen|step|note|tip|important|warning|itemizedlist|variablelist|orderedlist|title|procedure'

ENT_TOKEN = re.compile(
    r'<!ENTITY\s+(%\s+)?([\w.:-]+)\s+(?:SYSTEM\s+)?(["\'])(.*?)\3\s*>'
    r'|%([\w.-]+);', re.S)
NUMREF = re.compile(r'&#x?[0-9A-Fa-f]+;')


def ln(el):
    """Local name of an element, or '' for comments and PIs."""
    return etree.QName(el).localname if isinstance(el.tag, str) else ''


class Entities:
    def __init__(self, xmldir, start='generic-entities.ent'):
        self.dir = Path(xmldir)
        self.raw = {}
        self.local = {}        # entity name -> file that declares it locally
        self.conflicts = []
        self._load(start, {})
        self._kind = {}

    def _load(self, fn, pents):
        txt = re.sub(r'<!--.*?-->', '', (self.dir / fn).read_text(), flags=re.S)
        for m in ENT_TOKEN.finditer(txt):
            if m.group(5):                       # %pe; reference -> load file
                ref = pents.get(m.group(5))
                if ref:
                    self._load(ref, pents)
            elif m.group(1):                     # parameter entity declaration
                pents.setdefault(m.group(2), m.group(4))
            else:                                # first declaration wins (XML rule)
                self.raw.setdefault(m.group(2), m.group(4))

    def expand(self, name, seen=()):
        """Fully expand an entity value as XML text (nested refs resolved)."""
        if name in seen:
            return ''
        def rep(m):
            n = m.group(1)
            if n in BUILTIN:
                return m.group(0)
            if n not in self.raw:
                return f'&amp;{n};'   # undefined entity: keep visible
            return self.expand(n, seen + (name,))
        return re.sub(r'&([A-Za-z][\w.-]*);', rep, self.raw[name])

    def kind(self, name):
        """char | attr | variant | inline | block | unknown"""
        if name in self._kind:
            return self._kind[name]
        if name not in self.raw:
            k = 'unknown'
        else:
            raw, full = self.raw[name].strip(), self.expand(name)
            if NUMREF.fullmatch(raw) or (len(raw) == 1 and raw not in '<&'):
                k = 'char'
            elif re.search(rf'<({BLOCK_TAGS})[\s>]', full):
                k = 'block'
            elif re.search(r'\s(os|arch|condition)=', full):
                k = 'variant'
            elif '<' in full:
                k = 'inline'
            else:
                k = 'attr'
        self._kind[name] = k
        return k

    def char(self, name):
        raw = self.raw[name].strip()
        return raw if NUMREF.fullmatch(raw) else raw

    def fragment(self, name, marked=True):
        """Parse an entity value as an inline DocBook fragment."""
        val = self.raw[name] if marked else self.expand(name)
        if marked:
            val = self.mark(val)
        return etree.fromstring(
            f'<phrase xmlns="{DB}" xmlns:xlink="{XL}">{val}</phrase>')

    def plain(self, name, profile):
        """Plain-text value of an entity for one product profile."""
        frag = self.fragment(name, marked=False)
        profile_filter(frag, profile)
        return re.sub(r'\s+', ' ', ''.join(frag.itertext())).strip()

    def mark(self, txt):
        """Replace &ent; with markers; char entities become numeric refs."""
        def rep(m):
            n = m.group(1)
            if n in BUILTIN:
                return m.group(0)
            if n in self.raw and self.kind(n) == 'char':
                c = self.char(n)
                return c if NUMREF.fullmatch(c) else {'<': '&lt;', '&': '&amp;'}.get(c, c)
            return f'{M0}{n}{M1}'
        return re.sub(r'&([A-Za-z][\w.-]*);', rep, txt)


def parse_marked(path, ents):
    """Parse a DocBook file without DTD; entity refs become markers."""
    txt = Path(path).read_text()
    m = re.search(r'<!DOCTYPE\s[^\[>]*\[(.*?)\]\s*>', txt, flags=re.S)
    if m:                       # local entity declarations in the internal subset
        for d in ENT_TOKEN.finditer(m.group(1)):
            if d.group(2) and not d.group(1):
                name, val = d.group(2), d.group(4)
                if ents.raw.get(name, val) != val:
                    ents.conflicts.append(f'{Path(path).name}: &{name}; differs from global')
                ents.raw.setdefault(name, val)
                ents.local.setdefault(name, Path(path).name)
                ents._kind.pop(name, None)
    txt = re.sub(r'<!DOCTYPE\s[^\[>]*(\[.*?\])?\s*>', '', txt, count=1, flags=re.S)
    txt = ents.mark(txt)
    return etree.fromstring(txt.encode(), etree.XMLParser(remove_blank_text=False))


def keep(el, profile):
    for a in PROFILE_ATTRS:
        v = el.get(a)
        if v is not None and not (set(v.split(';')) & profile.get(a, set())):
            return False
    return True


def profile_filter(root, profile):
    for el in list(root.iter()):
        if isinstance(el.tag, str) and not keep(el, profile):
            parent = el.getparent()
            if parent is None:
                continue
            # keep tail text
            if el.tail:
                prev = el.getprevious()
                if prev is not None:
                    prev.tail = (prev.tail or '') + el.tail
                else:
                    parent.text = (parent.text or '') + el.tail
            parent.remove(el)


SLES_PROFILE = {
    'os': {'sles'},
    'arch': {'x86_64', 'zseries', 'power', 'aarch64'},
    'condition': {'suse-product'},
}
