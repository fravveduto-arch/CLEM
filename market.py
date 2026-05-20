"""
CLEM - Claude Energy Monitor
market.py — Aggiornamento reale PUN/PSV tramite Claude API (web search).

Cache giornaliera su file JSON locale per minimizzare le chiamate API.
Fallback su valori hardcoded se la chiamata fallisce.
"""

import json
import os
import re
from datetime import date
from anthropic import Anthropic

CACHE_FILE = os.path.join(os.path.dirname(__file__), ".market_cache.json")

FALLBACK = {
    'PUN_Luce': 0.1185,
    'PSV_Gas':  0.4650,
    'source':   'fallback',
    'date':     str(date.today()),
}


def _load_cache() -> dict | None:
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, 'r') as f:
            data = json.load(f)
        if data.get('date') == str(date.today()):
            return data
    except Exception:
        pass
    return None


def _save_cache(data: dict):
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass


def _extract_float(text: str, keyword: str) -> float | None:
    """Estrae il primo numero decimale dopo una keyword nel testo."""
    pattern = rf'{keyword}[^\d]{{0,30}}([\d]+[.,][\d]+)'
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(',', '.'))
        except ValueError:
            pass
    return None


def get_market_indices(api_key: str) -> dict:
    """
    Recupera PUN e PSV aggiornati tramite Claude API con web search.
    Usa cache giornaliera per non sprecare credito API.
    """
    cached = _load_cache()
    if cached:
        return cached

    try:
        client = Anthropic(api_key=api_key)
        today = str(date.today())

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=512,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{
                "role": "user",
                "content": (
                    f"Oggi è {today}. Cerca il valore attuale del PUN (Prezzo Unico Nazionale) "
                    f"dell'energia elettrica in Italia in €/kWh e il PSV (Punto di Scambio Virtuale) "
                    f"del gas naturale in €/Smc. "
                    f"Rispondi SOLO con due righe nel formato esatto:\n"
                    f"PUN: <valore>\n"
                    f"PSV: <valore>\n"
                    f"Usa valori numerici con il punto come separatore decimale."
                )
            }]
        )

        # Estrai il testo dalla risposta
        full_text = ""
        for block in response.content:
            if hasattr(block, 'text'):
                full_text += block.text + "\n"

        pun = _extract_float(full_text, r'PUN\s*[:\-]?\s*')
        psv = _extract_float(full_text, r'PSV\s*[:\-]?\s*')

        # Sanity check sui valori estratti
        if pun and 0.05 < pun < 0.80:
            pass
        else:
            pun = FALLBACK['PUN_Luce']

        if psv and 0.10 < psv < 2.50:
            pass
        else:
            psv = FALLBACK['PSV_Gas']

        result = {
            'PUN_Luce': pun,
            'PSV_Gas':  psv,
            'source':   'claude_websearch',
            'date':     today,
        }
        _save_cache(result)
        return result

    except Exception as e:
        print(f"[CLEM Market] Errore aggiornamento indici: {e}")
        return FALLBACK
