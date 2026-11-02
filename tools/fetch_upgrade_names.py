"""Build upgrade_names.json from the Skylanders Wiki (skylanders.fandom.com, CC-BY-SA): each character's
{{abilitiesbox}} lists its four basic upgrades, the Soul Gem, two named paths of three upgrades and its Wow Pows.
Run: python tools/fetch_upgrade_names.py   (writes upgrade_names.json next to portalforge.py)"""
import json
import os
import re
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = 'https://skylanders.fandom.com/api.php'


def get(**params):
    url = API + '?' + urllib.parse.urlencode({**params, 'format': 'json', 'formatversion': 2})
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'PortalSync upgrade names'}), timeout=60) as r:
        return json.load(r)


def pages(titles):
    """title -> wikitext, following redirects, 50 titles a request."""
    out = {}
    for i in range(0, len(titles), 50):
        q = get(action='query', prop='revisions', rvprop='content', rvslots='main', redirects=1, titles='|'.join(titles[i:i + 50]))['query']
        back = {r['to']: r['from'] for r in q.get('redirects', [])}
        norm = {n['to']: n['from'] for n in q.get('normalized', [])}
        for p in q['pages']:
            if 'revisions' in p:
                t = back.get(p['title'], p['title'])
                out[norm.get(t, t)] = p['revisions'][0]['slots']['main']['content']
    return out


def box(text):
    """The fields of the first {{abilitiesbox}}, as a dict (links and markup stripped)."""
    i = text.lower().find('{{abilitiesbox')
    if i < 0:
        return {}
    fields = {}
    for line in text[i:].splitlines()[1:]:
        if line.strip().startswith('}}'):
            break
        m = re.match(r'\|\s*([\w ]+?)\s*=\s*(.*)$', line.strip())
        if m:
            v = re.sub(r'\[\[(?:[^|\]]*\|)?([^\]]*)\]\]', r'\1', m.group(2))
            fields[m.group(1).lower()] = re.sub(r"'''?|<[^>]+>", '', v).strip()
    return fields


def main():
    names = json.load(open(os.path.join(HERE, 'figure_names.json'), encoding='utf-8'))
    chars = {}  # character id -> wiki page title (the plain name of variant 0; a Swapper's halves share a page)
    for key, name in names.items():
        fid, var = map(int, key.split(':'))
        if var == 0 or key.replace(f':{var}', ':0') not in names:
            chars.setdefault(fid, re.sub(r'\s*\([^)]*\)$', '', name))  # 'Bash (Legendary)', 'Blast Zone (Top)': the page
    text = pages(sorted(set(chars.values())))
    out = {}
    for fid, title in sorted(chars.items()):
        f = box(text.get(title, ''))
        half = 'top' if 2000 <= fid < 3000 else 'bottom' if 1000 <= fid < 2000 else ''
        p = (lambda k: f.get(half + k) or f.get(k, '')) if half else f.get
        basic = [p(f'basicupgrade{i}') or '' for i in range(1, 5)]
        if not any(basic):
            continue
        out[str(fid)] = {'page': title, 'basic': basic, 'soulgem': p('soulgemname') or '',
                         'paths': [p('path1name') or '', p('path2name') or ''],
                         'path1': [p(f'path1upgrade{i}') or '' for i in range(1, 4)],
                         'path2': [p(f'path2upgrade{i}') or '' for i in range(1, 4)],
                         'wowpow': p('wowpowname') or ''}
    with open(os.path.join(HERE, 'upgrade_names.json'), 'w', encoding='utf-8') as f:
        json.dump({'source': 'Skylanders Wiki (skylanders.fandom.com), CC-BY-SA 3.0', 'figures': out}, f, indent=1, ensure_ascii=False)
    missing = sorted(t for fid, t in chars.items() if str(fid) not in out)
    print(f'{len(out)} characters with upgrade names; {len(missing)} without: {", ".join(missing[:60])}')


if __name__ == '__main__':
    main()
