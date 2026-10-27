"""Kesici Takım Sabah Brifingi — her iş günü GitHub Actions tarafından çalıştırılır.
Tavily ile haber/fiyat tarar, Gemini Flash Lite ile süzer ve Türkçe özetler, docs/data altına yazar.
Gerekli ortam değişkenleri: TAVILY_API_KEY, GEMINI_API_KEY   (GitHub > Settings > Secrets)
"""
import json, os, re, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'docs' / 'data'
DATA.mkdir(parents=True, exist_ok=True)
TR = timezone(timedelta(hours=3))
TODAY = datetime.now(TR).strftime('%Y-%m-%d')
TAVILY = os.environ.get('TAVILY_API_KEY', '')
GEMINI = os.environ.get('GEMINI_API_KEY', '')
MODELS = [os.environ.get('GEMINI_MODEL', 'gemini-3.1-flash-lite'), 'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite']
MAX_ITEMS = 10      # sabah okunacak en fazla madde
MIN_SCORE = 6       # bu puanın altındaki haberler elenir

# ---------------------------------------------------------------- ilgi profili (süzgeç)
PROFILE = """Okuyucu: Türkiye'de havacılık için hassas talaşlı imalat yapan bir fabrikanın kesici takım satın alma mühendisi.
İşlenen malzemeler: alüminyum, Inconel, Hastelloy ve diğer nikel alaşımları, paslanmaz çelik (15-5PH, 416 vb.).
Satın alınan ürünler: karbür parmak frezeler, solid karbür matkaplar, torna/freze/matkap insertleri (seramik/SiAlON dahil),
kılavuzlar, diş frezeleri, takım tutucular (shrink-fit, hidrolik), aşındırıcı/çapak alma ürünleri.
Ana markalar: Kennametal, Iscar, Sandvik Coromant, Walter, Seco, TaeguTec, Tungaloy, Korloy, Ingersoll, NTK, Greenleaf,
Emuge, OSG, Guhring, YG-1, Haimer, Nikken, Big Kaiser, Albrecht.
Önemsediği konular: fiyat artışları/zam duyuruları ve tungsten ek ücretleri; tungsten (APT), kobalt ve karbür çubuk fiyatları;
Çin ihracat kısıtları ve gümrük/tarife değişiklikleri; teslim süresi/tedarik sorunları; markaların satın alma-birleşme haberleri;
nikel alaşımı ve alüminyum işlemede somut yeni takım/kalite/geometri lansmanları; havacılık üretim hızı (Airbus/Boeing) gibi talep sinyalleri;
Türkiye'deki bayi/distribütör değişiklikleri.
Önemsemediği: genel reklam metinleri, tarihsiz/somut olmayan içerik, ahşap/inşaat/hobi takımları, borsa yorumu."""

NEWS_QUERIES = [
    'cutting tools price increase Sandvik Coromant Kennametal Iscar Walter Seco',
    'tungsten surcharge carbide tools price',
    'tungsten APT price market China export',
    'cemented carbide rod supply price',
    'Kennametal OR Sandvik Coromant OR Iscar news',
    'new insert grade Inconel nickel alloy machining',
    'aerospace aluminum machining new end mill launch',
    'cutting tool industry acquisition merger',
    'Airbus Boeing production rate supply chain',
    'kesici takım zam fiyat artışı',
]
PRICE_QUERIES = {
    'tungsten': 'APT ammonium paratungstate price per mtu this week',
    'cobalt': 'cobalt metal price per tonne this week',
}

# ---------------------------------------------------------------- yardımcılar
def load(name, default):
    p = DATA / name
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else default

def save(name, obj):
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')

def tavily(query, news=True, n=6):
    body = {'query': query, 'max_results': n, 'search_depth': 'basic'}
    if news:
        body.update({'topic': 'news', 'days': 4})
    r = requests.post('https://api.tavily.com/search', json=body, timeout=60,
                      headers={'Authorization': f'Bearer {TAVILY}'})
    if r.status_code >= 400:
        print('Tavily hata', r.status_code, r.text[:200]); return []
    out = []
    for x in r.json().get('results', []):
        out.append({'url': x.get('url'), 'title': (x.get('title') or '').strip(),
                    'text': re.sub(r'\s+', ' ', x.get('content') or '')[:900],
                    'date': (x.get('published_date') or '')[:16]})
    return out

def gemini(prompt):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI)
    cfg = types.GenerateContentConfig(temperature=0.1, response_mime_type='application/json')
    last = None
    for attempt in range(6):
        model = MODELS[min(attempt // 2, len(MODELS) - 1)]
        try:
            txt = client.models.generate_content(model=model, contents=prompt, config=cfg).text
            txt = re.sub(r'^```(?:json)?|```$', '', txt.strip(), flags=re.M).strip()
            return json.loads(txt[txt.find('{'): txt.rfind('}') + 1])
        except Exception as e:
            last = e; msg = str(e); print('Gemini hata:', msg[:200])
            wait = re.search(r"retryDelay'?:\s*'?(\d+)", msg)
            time.sleep(int(wait.group(1)) + 2 if wait else 15)
    raise last

# ---------------------------------------------------------------- 1) kurlar (ECB, ücretsiz, anahtarsız)
def fx():
    out = {}
    for base in ('EUR', 'USD'):
        try:
            j = requests.get(f'https://api.frankfurter.app/latest?from={base}&to=TRY', timeout=30).json()
            out[f'{base}/TRY'] = {'value': j['rates']['TRY'], 'date': j['date'], 'unit': 'TRY',
                                  'source': 'https://www.frankfurter.app'}
        except Exception as e:
            print('Kur hatası', e)
    return out

# ---------------------------------------------------------------- 2) hammadde fiyatları (haberden çıkarım)
def metals():
    ev = []
    for k, q in PRICE_QUERIES.items():
        for x in tavily(q, news=True, n=5):
            ev.append({**x, 'metal': k})
    if not ev:
        return {}
    txt = '\n\n'.join(f"[{i+1}] ({e['metal']}) {e['title']} — {e['url']} — {e['date']}\n{e['text']}" for i, e in enumerate(ev))
    p = f"""Aşağıdaki arama sonuçlarından en güncel fiyatları çıkar. Bugün {TODAY}.
- tungsten: Çin veya Avrupa APT (ammonium paratungstate) fiyatı, USD/mtu tercih edilir.
- cobalt: kobalt metal fiyatı, USD/ton tercih edilir.
Sadece metinde açıkça yazan rakamı kullan, tahmin etme. 10 günden eski ise veya bulamazsan value null olsun.
JSON: {{"tungsten": {{"value": number|null, "unit": "...", "date": "YYYY-MM-DD", "source": "URL", "note_tr": "kısa"}},
        "cobalt": {{"value": number|null, "unit": "...", "date": "YYYY-MM-DD", "source": "URL", "note_tr": "kısa"}}}}

{txt}"""
    try:
        res = gemini(p)
    except Exception as e:
        print('Fiyat çıkarılamadı', e); return {}
    urls = {e['url'] for e in ev}
    return {k: v for k, v in res.items() if isinstance(v, dict) and v.get('value') and v.get('source') in urls}

# ---------------------------------------------------------------- 3) haberler
def news(seen):
    pool, keys = [], set()
    for q in NEWS_QUERIES:
        for x in tavily(q):
            key = re.sub(r'\W+', '', x['title'].lower())[:80]
            if not x['url'] or x['url'] in seen or key in keys:
                continue
            keys.add(key); pool.append(x)
    print(len(pool), 'yeni aday haber')
    if not pool:
        return [], []
    txt = '\n\n'.join(f"[{i}] {x['title']} — {x['url']} — {x['date']}\n{x['text']}" for i, x in enumerate(pool))
    p = f"""{PROFILE}

Bugün {TODAY}. Aşağıdaki haber adaylarını bu okuyucu için değerlendir.
Her biri için 0-10 arası "score" ver (10 = işini/fiyatlarını doğrudan etkileyen somut ve yeni bilgi; 0 = alakasız).
Aynı olayı anlatan haberlerden sadece en iyisini puanla, diğerlerine 0 ver. Somut rakam/tarih içermeyen genel içeriğe en fazla 4 ver.
Puanı {MIN_SCORE} ve üstü olanlar için Türkçe yaz:
- "title_tr": kısa başlık
- "summary_tr": 1-2 cümle, somut bilgi (rakam, tarih, marka)
- "why_tr": bu satın alma mühendisi için neden önemli / ne yapmalı (1 cümle)
- "category": "zam" | "hammadde" | "tedarik" | "teknoloji" | "sektor" | "talep"
JSON: {{"items": [{{"i": index, "score": n, "title_tr": "...", "summary_tr": "...", "why_tr": "...", "category": "..."}}]}}

ADAYLAR:
{txt}"""
    res = gemini(p)
    items = []
    for it in res.get('items', []):
        try:
            src = pool[int(it['i'])]
        except Exception:
            continue
        if it.get('score', 0) >= MIN_SCORE and it.get('title_tr'):
            items.append({'score': it['score'], 'category': it.get('category', 'sektor'), 'title': it['title_tr'],
                          'summary': it.get('summary_tr', ''), 'why': it.get('why_tr', ''), 'url': src['url'],
                          'source_title': src['title'], 'date': src['date']})
    items.sort(key=lambda x: -x['score'])
    return items[:MAX_ITEMS], [x['url'] for x in pool]

# ---------------------------------------------------------------- ana akış
def main():
    if not (TAVILY and GEMINI):
        sys.exit('TAVILY_API_KEY ve GEMINI_API_KEY tanımlı olmalı.')
    seen = load('seen.json', [])
    prices = load('prices.json', {})          # {"EUR/TRY": [{"date":..,"value":..}], ...}

    today_prices = {**fx(), **metals()}
    for k, v in today_prices.items():
        hist = [h for h in prices.get(k, []) if h['date'] != v['date']]
        hist.append({'date': v['date'], 'value': v['value'], 'unit': v.get('unit'), 'source': v.get('source'),
                     'note': v.get('note_tr', '')})
        prices[k] = sorted(hist, key=lambda h: h['date'])[-400:]
    save('prices.json', prices)

    try:
        items, urls = news(set(seen))
    except Exception as e:
        print('Haber adımı hata:', e); items, urls = [], []
    save(f'brief-{TODAY}.json', {'date': TODAY, 'generated': datetime.now(TR).strftime('%Y-%m-%d %H:%M'),
                                 'items': items})
    seen = (seen + urls)[-3000:]
    save('seen.json', seen)

    idx = load('index.json', [])
    if TODAY not in idx:
        idx.append(TODAY)
    save('index.json', sorted(idx, reverse=True)[:180])
    print(f'{TODAY}: {len(items)} madde yazıldı.')

if __name__ == '__main__':
    main()
