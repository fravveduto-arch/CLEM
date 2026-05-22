"""
CLEM - Claude Energy Monitor
market.py — Aggiornamento PUN/PSV (cache giornaliera) e offerte ARERA (cache 15 giorni).
"""

import json
import os
import re
from datetime import date, timedelta
from anthropic import Anthropic

CACHE_FILE   = os.path.join(os.path.dirname(__file__), ".market_cache.json")
OFFERS_CACHE = os.path.join(os.path.dirname(__file__), ".offers_cache.json")
OFFERS_TTL_DAYS = 15

FALLBACK = {
    'PUN_Luce': 0.1185,
    'PSV_Gas':  0.4650,
    'source':   'fallback',
    'date':     str(date.today()),
}

# Offerte di fallback statiche usate se la chiamata API fallisce
FALLBACK_OFFERS = {
    'luce': [
        {'name': 'Enel Luce Flex',         'type': 'Variabile', 'spread': 0.010, 'fixed': 10.0, 'dual_fuel': True},
        {'name': 'Edison Dynamic Luce',    'type': 'Variabile', 'spread': 0.014, 'fixed': 12.0, 'dual_fuel': True},
        {'name': 'Eni Plenitude Luce',     'type': 'Variabile', 'spread': 0.012, 'fixed': 11.0, 'dual_fuel': True},
        {'name': 'A2A Luce Smart',         'type': 'Variabile', 'spread': 0.010, 'fixed': 10.5, 'dual_fuel': True},
        {'name': 'Illumia Luce Web',       'type': 'Fisso',     'price':  0.118, 'fixed': 9.5,  'dual_fuel': True},
        {'name': 'Hera Luce Controllo',    'type': 'Variabile', 'spread': 0.009, 'fixed': 10.0, 'dual_fuel': True},
        {'name': 'Iren Luce',              'type': 'Fisso',     'price':  0.105, 'fixed': 9.0,  'dual_fuel': False},
        {'name': 'Acea Luce Web',          'type': 'Fisso',     'price':  0.122, 'fixed': 10.0, 'dual_fuel': True},
        {'name': 'Green Network Luce',     'type': 'Variabile', 'spread': 0.008, 'fixed': 9.0,  'dual_fuel': True},
    ],
    'gas': [
        {'name': 'Enel Gas Flex',          'type': 'Variabile', 'spread': 0.035, 'fixed': 10.0, 'dual_fuel': True},
        {'name': 'Edison Dynamic Gas',     'type': 'Variabile', 'spread': 0.067, 'fixed': 12.0, 'dual_fuel': True},
        {'name': 'Eni Plenitude Gas',      'type': 'Variabile', 'spread': 0.040, 'fixed': 11.0, 'dual_fuel': True},
        {'name': 'A2A Gas Smart',          'type': 'Variabile', 'spread': 0.038, 'fixed': 10.5, 'dual_fuel': True},
        {'name': 'Illumia Gas Web',        'type': 'Fisso',     'price':  0.475, 'fixed': 10.0, 'dual_fuel': True},
        {'name': 'Hera Gas Controllo',     'type': 'Variabile', 'spread': 0.035, 'fixed': 12.0, 'dual_fuel': True},
        {'name': 'Iren Gas',               'type': 'Fisso',     'price':  0.460, 'fixed': 9.0,  'dual_fuel': False},
        {'name': 'Acea Gas Web',           'type': 'Fisso',     'price':  0.468, 'fixed': 10.0, 'dual_fuel': True},
        {'name': 'Green Network Gas',      'type': 'Variabile', 'spread': 0.032, 'fixed': 9.5,  'dual_fuel': True},
    ],
}


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _load_cache(path: str, ttl_days: int = 1) -> dict | None:
    try:
        if not os.path.exists(path):
            return None
        with open(path, 'r') as f:
            data = json.load(f)
        cached_date = date.fromisoformat(data.get('date', '2000-01-01'))
        if (date.today() - cached_date).days < ttl_days:
            return data
    except Exception:
        pass
    return None


def _save_cache(path: str, data: dict):
    try:
        with open(path, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _extract_float(text: str, keyword: str) -> float | None:
    pattern = rf'{keyword}[^\d]{{0,30}}([\d]+[.,][\d]+)'
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(',', '.'))
        except ValueError:
            pass
    return None


# ── PUN / PSV ────────────────────────────────────────────────────────────────

def get_market_indices(api_key: str) -> dict:
    cached = _load_cache(CACHE_FILE, ttl_days=1)
    if cached and 'PUN_Luce' in cached:
        return cached

    try:
        client = Anthropic(api_key=api_key)
        today = str(date.today())
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=512,
            tools=[{"type": "web_search_20250105", "name": "web_search"}],
            messages=[{"role": "user", "content": (
                f"Oggi e' {today}. Cerca il valore attuale del PUN (Prezzo Unico Nazionale) "
                f"dell'energia elettrica in Italia in EUR/kWh e il PSV (Punto di Scambio Virtuale) "
                f"del gas naturale in EUR/Smc. "
                f"Rispondi SOLO con due righe nel formato esatto:\n"
                f"PUN: <valore>\nPSV: <valore>\n"
                f"Usa valori numerici con il punto come separatore decimale."
            )}]
        )
        full_text = "".join(b.text for b in response.content if hasattr(b, 'text'))
        pun = _extract_float(full_text, r'PUN\s*[:\-]?\s*')
        psv = _extract_float(full_text, r'PSV\s*[:\-]?\s*')
        if not (pun and 0.05 < pun < 0.80):
            pun = FALLBACK['PUN_Luce']
        if not (psv and 0.10 < psv < 2.50):
            psv = FALLBACK['PSV_Gas']
        result = {'PUN_Luce': pun, 'PSV_Gas': psv, 'source': 'claude_websearch', 'date': today}
        _save_cache(CACHE_FILE, result)
        return result
    except Exception as e:
        print(f"[CLEM Market] Errore indici: {e}")
        return FALLBACK


# ── Offerte di mercato ────────────────────────────────────────────────────────

def get_market_offers(api_key: str) -> dict:
    """
    Recupera offerte luce/gas aggiornate da ARERA tramite Claude API.
    Cache 15 giorni. Fallback su offerte statiche.
    Restituisce dict con chiavi 'luce', 'gas', 'source', 'date'.
    """
    cached = _load_cache(OFFERS_CACHE, ttl_days=OFFERS_TTL_DAYS)
    if cached and 'luce' in cached:
        return cached

    try:
        client = Anthropic(api_key=api_key)
        today = str(date.today())

        prompt = (
            f"Oggi e' {today}. Cerca sul portale offerte ARERA (arera.it) o su siti comparatori "
            f"italiani (segugio.it, facile.it, sostariffe.it) le migliori offerte luce e gas "
            f"attualmente disponibili in Italia per uso domestico dei seguenti fornitori: "
            f"Enel, Edison, Eni Plenitude, A2A, Illumia, Hera Comm, Iren, Acea, Green Network. "
            f"Per ciascun fornitore indica se l'offerta e' solo luce, solo gas, o dual fuel. "
            f"Nota: Iren NON propone offerte dual fuel, solo luce o solo gas separatamente. "
            f"Rispondi SOLO con un oggetto JSON valido, senza testo aggiuntivo, con questa struttura:\n"
            f'{{"luce": [{{"name": "Nome Offerta", "fornitore": "Enel", "type": "Variabile", '
            f'"spread": 0.010, "price": 0, "fixed": 10.0, "dual_fuel": true, "bonus": 0}}], '
            f'"gas": [{{"name": "Nome Offerta", "fornitore": "Enel", "type": "Variabile", '
            f'"spread": 0.035, "price": 0, "fixed": 10.0, "dual_fuel": true, "bonus": 0}}]}}\n'
            f"Usa spread per offerte variabili (indicizzate PUN/PSV), price per offerte a prezzo fisso. "
            f"Se spread non disponibile metti 0, se price non disponibile metti 0. "
            f"Includi almeno 6 offerte luce e 6 offerte gas."
        )

        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=2000,
            tools=[{"type": "web_search_20250105", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}]
        )

        full_text = "".join(b.text for b in response.content if hasattr(b, 'text'))

        # Estrai JSON dalla risposta
        json_match = re.search(r'\{[\s\S]*\}', full_text)
        if json_match:
            offers_data = json.loads(json_match.group(0))
            # Validazione minima
            if 'luce' in offers_data and 'gas' in offers_data:
                offers_data['source'] = 'arera_websearch'
                offers_data['date'] = today
                _save_cache(OFFERS_CACHE, offers_data)
                return offers_data

    except Exception as e:
        print(f"[CLEM Offers] Errore recupero offerte: {e}")

    # Fallback
    result = dict(FALLBACK_OFFERS)
    result['source'] = 'fallback'
    result['date'] = str(date.today())
    return result
