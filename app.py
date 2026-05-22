"""
CLEM — Claude Energy Monitor
app.py — Applicazione principale Streamlit.

Funzionalità:
  • Tab personale: carica PDF bollette, archivio locale, analisi tecnica, simulazione dual fuel
  • Tab ospite:    carica PDF oppure inserimento manuale, stessa simulazione
  • Aggiornamento PUN/PSV reale via Claude API (cache giornaliera)
  • Storico consumi con grafico
  • Alert risparmio automatico
  • Condivisione risultato via URL query string
  • Download report PDF
"""

import streamlit as st
import pandas as pd
import io
import os
import json
from datetime import date
from urllib.parse import urlencode, parse_qs, urlparse

from fpdf import FPDF
from parser import EnergyParser
from market import get_market_indices, get_market_offers, FALLBACK, FALLBACK_OFFERS

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CLEM — Claude Energy Monitor",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;600&family=Space+Mono:wght@400;700&display=swap');

html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }
h1, h2, h3 { font-family: 'Space Mono', monospace; letter-spacing: -0.03em; }

.metric-card {
    background: linear-gradient(135deg, #0f1923 0%, #1a2d3d 100%);
    border: 1px solid #2a4a6b;
    border-radius: 12px;
    padding: 1.2rem 1.5rem;
    margin-bottom: 0.8rem;
}
.alert-saving {
    background: linear-gradient(90deg, #0d3320, #0a4a2a);
    border-left: 4px solid #00e676;
    border-radius: 8px;
    padding: 1rem 1.5rem;
    color: #00e676;
    font-family: 'Space Mono', monospace;
    font-size: 1.1rem;
    margin: 1rem 0;
}
.alert-neutral {
    background: linear-gradient(90deg, #1a2a1a, #2a3a2a);
    border-left: 4px solid #ffb300;
    border-radius: 8px;
    padding: 1rem 1.5rem;
    color: #ffb300;
    font-family: 'Space Mono', monospace;
    font-size: 0.95rem;
    margin: 1rem 0;
}
.share-box {
    background: #0f1923;
    border: 1px dashed #2a4a6b;
    border-radius: 8px;
    padding: 0.8rem 1rem;
    font-family: 'Space Mono', monospace;
    font-size: 0.8rem;
    color: #7faacc;
    word-break: break-all;
    margin-top: 0.5rem;
}
.vendor-badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 0.75rem;
    font-weight: 600;
    background: #1a3a5c;
    color: #7faacc;
    margin-left: 8px;
}
.stTabs [data-baseweb="tab"] {
    font-family: 'Space Mono', monospace;
    font-size: 0.9rem;
}
</style>
""", unsafe_allow_html=True)

# ── Costanti ──────────────────────────────────────────────────────────────────
FIXED_COSTS = {
    'Edison':        {'Luce': 17.21, 'Gas': 13.87},
    'Enel':          {'Luce': 15.50, 'Gas': 12.90},
    'Eni Plenitude': {'Luce': 14.80, 'Gas': 12.50},
    'A2A':           {'Luce': 15.00, 'Gas': 13.00},
    'Illumia':       {'Luce': 12.90, 'Gas': 11.50},
    'Hera':          {'Luce': 13.50, 'Gas': 12.00},
    'Iren':          {'Luce': 13.00, 'Gas': 11.80},
    'Acea':          {'Luce': 14.50, 'Gas': 12.70},
    'Green Network': {'Luce': 11.90, 'Gas': 10.80},
    'Altro':         {'Luce': 15.00, 'Gas': 13.00},
}

SAVINGS_ALERT_THRESHOLD = 80  # €/anno


# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def annual_projection(consumption: float, billing_days: int, period_type: str) -> tuple[float, str, str]:
    """
    Calcola la proiezione annua del consumo.
    Restituisce (valore_annuo, moltiplicatore_usato, fonte).
    """
    if billing_days > 10:
        # Rilevato dal PDF: proiezione precisa
        annual = consumption * (365 / billing_days)
        label = f"{billing_days}gg rilevati da bolletta"
        return round(annual, 1), f"x{365/billing_days:.1f}", label
    elif period_type == 'mensile':
        return round(consumption * 12, 1), "x12", "mensile (da testo bolletta)"
    elif period_type == 'trimestrale':
        return round(consumption * 4, 1), "x4", "trimestrale (da testo bolletta)"
    elif period_type == 'bimestrale':
        return round(consumption * 6, 1), "x6", "bimestrale (default)"
    else:
        # Euristica: se consumo basso -> mensile, altrimenti bimestrale
        if consumption < 150:
            return round(consumption * 12, 1), "x12", "stimato mensile"
        else:
            return round(consumption * 6, 1), "x6", "stimato bimestrale"


def compute_annual_cost(luce_kwh, gas_smc, indices, luce_spread, gas_spread,
                        luce_fixed_month, gas_fixed_month) -> float:
    cost_l = (luce_kwh * (indices['PUN_Luce'] * 1.1 + luce_spread)) + (luce_fixed_month * 12) if luce_kwh > 0 else 0
    cost_g = (gas_smc  * (indices['PSV_Gas']  + gas_spread))         + (gas_fixed_month  * 12) if gas_smc > 0 else 0
    return cost_l + cost_g


def compute_offer_cost(offer, luce_kwh, gas_smc, indices) -> float:
    if offer['type'] == 'Variabile':
        cost_l = (luce_kwh * (indices['PUN_Luce'] * 1.1 + offer['luce_spread'])) + (offer['luce_fixed'] * 12)
        cost_g = (gas_smc  * (indices['PSV_Gas']  + offer['gas_spread']))         + (offer['gas_fixed']  * 12)
    else:
        cost_l = (luce_kwh * offer['luce_price']) + (offer['luce_fixed'] * 12)
        cost_g = (gas_smc  * offer['gas_price'])  + (offer['gas_fixed']  * 12)
    return cost_l + cost_g - offer.get('bonus', 0)


def sort_by_date(candidates: pd.DataFrame):
    if candidates.empty:
        return None
    c = candidates.copy()
    c['_dp'] = pd.to_datetime(c['bill_date'], format='%d/%m/%Y', errors='coerce')
    return c.sort_values('_dp', ascending=False).iloc[0]


def _build_results_luce(offers_luce, current_annual_luce, luce_kwh, indices, extra=None):
    results = []
    all_offers = offers_luce + (extra or [])
    for o in all_offers:
        if o['type'] == 'Variabile':
            cost = (luce_kwh * (indices['PUN_Luce'] * 1.1 + o.get('spread', 0))) + (o.get('fixed', 10) * 12)
        else:
            cost = (luce_kwh * o.get('price', indices['PUN_Luce'] * 1.1)) + (o.get('fixed', 10) * 12)
        cost -= o.get('bonus', 0)
        risparmio = current_annual_luce - cost
        results.append({
            'Offerta':            o['name'],
            'Tipo':               o['type'],
            'Solo Dual Fuel':     'SI' if o.get('dual_fuel') else 'NO',
            'Costo Annuo':        f"€ {cost:,.2f}",
            'Risparmio Annuo':    f"€ {risparmio:,.2f}",
        })
    results.sort(key=lambda x: -float(x['Risparmio Annuo'].replace('€','').replace(',','').strip()))
    return results


def _build_results_gas(offers_gas, current_annual_gas, gas_smc, indices, extra=None):
    results = []
    all_offers = offers_gas + (extra or [])
    for o in all_offers:
        if o['type'] == 'Variabile':
            cost = (gas_smc * (indices['PSV_Gas'] + o.get('spread', 0))) + (o.get('fixed', 10) * 12)
        else:
            cost = (gas_smc * o.get('price', indices['PSV_Gas'])) + (o.get('fixed', 10) * 12)
        cost -= o.get('bonus', 0)
        risparmio = current_annual_gas - cost
        results.append({
            'Offerta':            o['name'],
            'Tipo':               o['type'],
            'Solo Dual Fuel':     'SI' if o.get('dual_fuel') else 'NO',
            'Costo Annuo':        f"€ {cost:,.2f}",
            'Risparmio Annuo':    f"€ {risparmio:,.2f}",
        })
    results.sort(key=lambda x: -float(x['Risparmio Annuo'].replace('€','').replace(',','').strip()))
    return results


def _build_results_dual(offers_luce, offers_gas, current_annual, luce_kwh, gas_smc, indices):
    """Combina le offerte dual fuel abbinando per fornitore quando possibile."""
    dual_luce = {o['name']: o for o in offers_luce if o.get('dual_fuel')}
    dual_gas  = {o['name']: o for o in offers_gas  if o.get('dual_fuel')}

    # Raggruppa per fornitore (primo termine del nome)
    fornitori = {}
    for name, o in dual_luce.items():
        f = o.get('fornitore', name.split()[0])
        fornitori.setdefault(f, {})['luce'] = o
    for name, o in dual_gas.items():
        f = o.get('fornitore', name.split()[0])
        fornitori.setdefault(f, {})['gas'] = o

    results = []
    for fornitore, bundle in fornitori.items():
        ol = bundle.get('luce')
        og = bundle.get('gas')
        if not ol or not og:
            continue
        if ol['type'] == 'Variabile':
            cl = (luce_kwh * (indices['PUN_Luce'] * 1.1 + ol.get('spread', 0))) + (ol.get('fixed', 10) * 12)
        else:
            cl = (luce_kwh * ol.get('price', indices['PUN_Luce'] * 1.1)) + (ol.get('fixed', 10) * 12)
        if og['type'] == 'Variabile':
            cg = (gas_smc * (indices['PSV_Gas'] + og.get('spread', 0))) + (og.get('fixed', 10) * 12)
        else:
            cg = (gas_smc * og.get('price', indices['PSV_Gas'])) + (og.get('fixed', 10) * 12)
        total = cl + cg - ol.get('bonus', 0) - og.get('bonus', 0)
        risparmio = current_annual - total
        results.append({
            'Fornitore':          fornitore,
            'Offerta Luce':       ol['name'],
            'Offerta Gas':        og['name'],
            'Costo Annuo Dual':   f"€ {total:,.2f}",
            'Risparmio Annuo':    f"€ {risparmio:,.2f}",
        })
    results.sort(key=lambda x: -float(x['Risparmio Annuo'].replace('€','').replace(',','').strip()))
    return results


def render_simulation_tabs(current_annual, luce_kwh, gas_smc,
                            luce_annual, gas_annual,
                            luce_spread, gas_spread,
                            luce_fixed, gas_fixed,
                            indices, market_offers, extra_offers=None):
    """Renderizza le 3 tab Solo Luce / Solo Gas / Dual Fuel."""

    offers_luce = market_offers.get('luce', FALLBACK_OFFERS['luce'])
    offers_gas  = market_offers.get('gas',  FALLBACK_OFFERS['gas'])

    src = market_offers.get('source', 'fallback')
    src_label = "Offerte aggiornate da ARERA/comparatori" if src == 'arera_websearch' else "Offerte di riferimento (aggiornamento in corso)"
    st.caption(f"📋 {src_label} | Aggiornamento ogni 15 giorni")

    tab_l, tab_g, tab_d = st.tabs(["⚡ Solo Luce", "🔥 Solo Gas", "🔌 Dual Fuel"])

    best_offer_name = ""
    best_saving     = 0.0
    res_df_main     = pd.DataFrame()

    with tab_l:
        annual_luce_cost = (luce_kwh * (indices['PUN_Luce'] * 1.1 + luce_spread)) + (luce_fixed * 12)
        st.metric("Spesa annua attuale (solo luce)", f"€ {annual_luce_cost:,.2f}")
        res = _build_results_luce(offers_luce, annual_luce_cost, luce_kwh, indices)
        df_l = pd.DataFrame(res)
        st.dataframe(df_l, use_container_width=True, hide_index=True)
        if res:
            best_l = res[0]
            saving_l = float(best_l['Risparmio Annuo'].replace('€','').replace(',','').strip())
            render_alert(best_l['Offerta'], saving_l)
            res_df_main = df_l
            best_offer_name = best_l['Offerta']
            best_saving = saving_l

    with tab_g:
        annual_gas_cost = (gas_smc * (indices['PSV_Gas'] + gas_spread)) + (gas_fixed * 12)
        st.metric("Spesa annua attuale (solo gas)", f"€ {annual_gas_cost:,.2f}")
        res = _build_results_gas(offers_gas, annual_gas_cost, gas_smc, indices)
        df_g = pd.DataFrame(res)
        st.dataframe(df_g, use_container_width=True, hide_index=True)
        if res:
            best_g = res[0]
            saving_g = float(best_g['Risparmio Annuo'].replace('€','').replace(',','').strip())
            render_alert(best_g['Offerta'], saving_g)

    with tab_d:
        st.metric("Spesa annua attuale (luce + gas)", f"€ {current_annual:,.2f}")
        st.caption("Nota: Iren non propone offerte dual fuel e non compare in questa tab.")
        res = _build_results_dual(offers_luce, offers_gas, current_annual, luce_kwh, gas_smc, indices)
        if res:
            df_d = pd.DataFrame(res)
            st.dataframe(df_d, use_container_width=True, hide_index=True)
            best_d = res[0]
            saving_d = float(best_d['Risparmio Annuo'].replace('€','').replace(',','').strip())
            render_alert(best_d['Fornitore'], saving_d)
        else:
            st.info("Nessuna offerta dual fuel disponibile al momento.")

    return res_df_main, best_offer_name, best_saving


def render_comparison_table(current_annual, luce_kwh, gas_smc, indices, extra_offers=None):
    """Mantenuto per compatibilità con tab ospite — usa offerte fallback."""
    offers_luce = FALLBACK_OFFERS['luce']
    offers_gas  = FALLBACK_OFFERS['gas']
    res = _build_results_dual(offers_luce, offers_gas, current_annual, luce_kwh, gas_smc, indices)
    if not res:
        return pd.DataFrame(), '', 0.0
    df = pd.DataFrame(res)
    st.dataframe(df, use_container_width=True, hide_index=True)
    best = res[0]
    best_saving = float(best['Risparmio Annuo'].replace('€','').replace(',','').strip())
    return df, best['Fornitore'], best_saving


def render_alert(best_offer_name: str, best_saving: float):
    if best_saving >= SAVINGS_ALERT_THRESHOLD:
        st.markdown(f"""
        <div class="alert-saving">
        💡 RISPARMIO RILEVATO — Passando a <strong>{best_offer_name}</strong>
        potresti risparmiare circa <strong>€ {best_saving:,.0f} / anno</strong>.
        </div>
        """, unsafe_allow_html=True)
    elif best_saving > 0:
        st.markdown(f"""
        <div class="alert-neutral">
        ℹ️ Risparmio marginale rilevato (€ {best_saving:,.0f}/anno con {best_offer_name}).
        Potrebbe non valere i costi di switching.
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown("""
        <div class="alert-neutral">
        ✅ La tua tariffa attuale è già competitiva rispetto alle offerte censite.
        </div>
        """, unsafe_allow_html=True)


def render_share_link(luce_kwh, gas_smc, luce_spread, gas_spread,
                      luce_fixed, gas_fixed, vendor):
    params = {
        'luce': round(luce_kwh / 6),
        'gas':  round(gas_smc  / 6),
        'ls':   luce_spread,
        'gs':   gas_spread,
        'lf':   luce_fixed,
        'gf':   gas_fixed,
        'v':    vendor,
    }
    base_url = st.query_params.get("base_url", "http://localhost:8501")
    link = f"{base_url}?{urlencode(params)}"
    st.markdown("**🔗 Condividi questa analisi:**")
    st.markdown(f'<div class="share-box">{link}</div>', unsafe_allow_html=True)
    st.caption("Invia questo link a un amico: aprirà CLEM con i tuoi stessi parametri precompilati.")


def generate_pdf_report(vendor, luce_data: dict, gas_data: dict,
                        comparison_df: pd.DataFrame, indices: dict,
                        annual_cost: float) -> bytes:
    pdf = FPDF()
    
    # Registrazione font Unicode
    font_dir = os.path.join(os.path.dirname(__file__), "fonts")
    pdf.add_font("DejaVu", "", os.path.join(font_dir, "DejaVuSans.ttf"), unicode=True)
    pdf.add_font("DejaVu", "B", os.path.join(font_dir, "DejaVuSans-Bold.ttf"), unicode=True)
    
    pdf.add_page()
    pdf.set_font("DejaVu", "B", 18)
    pdf.cell(0, 12, "CLEM - Claude Energy Monitor", ln=True, align="C")
    
    pdf.set_font("DejaVu", "", 9)
    report_date = date.today().strftime('%d/%m/%Y')
    pun = indices.get('PUN_Luce', 0)
    psv = indices.get('PSV_Gas', 0)
    source = indices.get('source', '—')
    
    header_text = f"Report generato il {report_date} | PUN: {pun:.4f} €/kWh | PSV: {psv:.4f} €/Smc | Fonte: {source}"
    pdf.cell(0, 5, header_text, ln=True, align="C")
    pdf.ln(6)

    # Sezione fornitore attuale
    pdf.set_font("DejaVu", "B", 12)
    pdf.set_fill_color(15, 25, 35)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 9, f"  Fornitore attuale: {vendor}", ln=True, fill=True)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    def section_table(title, rows):
        pdf.set_font("DejaVu", "B", 11)
        pdf.cell(0, 8, title, ln=True)
        pdf.set_font("DejaVu", "", 9)
        pdf.set_fill_color(230, 235, 240)
        pdf.cell(70, 7, "Voce", 1, 0, "C", True)
        pdf.cell(60, 7, "Valore", 1, 0, "C", True)
        pdf.cell(55, 7, "Note", 1, 1, "C", True)
        for r in rows:
            pdf.cell(70, 7, r[0], 1)
            pdf.cell(60, 7, r[1], 1)
            pdf.cell(55, 7, r[2], 1, 1)
        pdf.ln(3)

    if luce_data:
        section_table("Energia Elettrica", [
            ["PUN (Mercato)", f"~{pun:.4f} €/kWh", "Aggiornato"],
            ["Perdite di rete", "+10%", "Moltiplicatore 1,10"],
            ["Spread", f"{luce_data.get('spread',0):.4f} €/kWh", "Da contratto"],
            ["Quota fissa", f"{luce_data.get('fixed_cost',0):.2f} €/mese", "PCV + Potenza"],
        ])

    if gas_data:
        section_table("Gas Naturale", [
            ["PSV (Mercato)", f"{psv:.4f} €/Smc", "Aggiornato"],
            ["Spread", f"{gas_data.get('spread',0):.4f} €/Smc", "Da contratto"],
            ["Quota fissa", f"{gas_data.get('fixed_cost',0):.2f} €/mese", "QVD + Quote"],
        ])

    # Spesa annua attuale
    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(0, 8, f"Spesa Annua Stimata (attuale): € {annual_cost:,.2f}", ln=True)
    pdf.ln(4)

    # Tabella confronto
    pdf.set_font("DejaVu", "B", 11)
    pdf.cell(0, 8, "Confronto Offerte di Mercato", ln=True)
    pdf.set_font("DejaVu", "", 8)
    pdf.set_fill_color(230, 235, 240)
    pdf.cell(75, 6, "Offerta", 1, 0, "C", True)
    pdf.cell(25, 6, "Tipo", 1, 0, "C", True)
    pdf.cell(45, 6, "Costo Annuo", 1, 0, "C", True)
    pdf.cell(40, 6, "Risparmio", 1, 1, "C", True)
    pdf.set_font("DejaVu", "", 8)
    for _, row in comparison_df.iterrows():
        # Gestione nomi colonne diversi tra tab Luce/Gas e Dual Fuel
        offerta = str(row.get('Offerta', row.get('Fornitore', '')))
        tipo = str(row.get('Tipo', 'Dual Fuel'))
        costo = str(row.get('Costo Annuo', row.get('Costo Annuo Dual', ''))).replace('€', '€')
        risparmio = str(row.get('Risparmio Annuo', '')).replace('€', '€')
        
        pdf.cell(75, 6, offerta, 1)
        pdf.cell(25, 6, tipo, 1)
        pdf.cell(45, 6, costo, 1)
        pdf.cell(40, 6, risparmio, 1, 1)

    pdf.ln(6)
    pdf.set_font("DejaVu", "", 8)
    pdf.multi_cell(0, 5,
        "Nota: i valori sono stime basate sui consumi delle ultime bollette estrapolati su base annua. "
        "I prezzi di mercato sono aggiornati alla data del report. Le offerte sono indicative; "
        "verificare sempre le condizioni contrattuali sul sito del fornitore.")

    return bytes(pdf.output())


# ── API Key ───────────────────────────────────────────────────────────────────

def get_api_key() -> str:
    # Priorità: secrets Streamlit Cloud > variabile ambiente > sidebar input
    if hasattr(st, 'secrets') and 'ANTHROPIC_API_KEY' in st.secrets:
        return st.secrets['ANTHROPIC_API_KEY']
    env_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if env_key:
        return env_key
    return st.session_state.get('api_key', '')


# ── Query string pre-fill (share link) ───────────────────────────────────────

def get_prefill() -> dict:
    params = st.query_params
    if not params:
        return {}
    return {
        'luce':   safe_float(params.get('luce', 0)),
        'gas':    safe_float(params.get('gas',  0)),
        'ls':     safe_float(params.get('ls',   0)),
        'gs':     safe_float(params.get('gs',   0)),
        'lf':     safe_float(params.get('lf',   0)),
        'gf':     safe_float(params.get('gf',   0)),
        'vendor': params.get('v', 'Altro'),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN APP
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("# ⚡ CLEM")
st.markdown("##### Claude Energy Monitor — Analisi e comparazione bollette energia")
st.markdown("---")

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Configurazione")

    # API Key
    api_key = get_api_key()
    if not api_key:
        api_key_input = st.text_input(
            "🔑 Anthropic API Key",
            type="password",
            help="Necessaria per aggiornare PUN/PSV in tempo reale"
        )
        if api_key_input:
            st.session_state['api_key'] = api_key_input
            api_key = api_key_input

    # Indici di mercato
    st.markdown("### 📈 Indici di Mercato")
    if api_key:
        with st.spinner("Aggiornamento prezzi..."):
            indices = get_market_indices(api_key)
        with st.spinner("Aggiornamento offerte..."):
            market_offers = get_market_offers(api_key)
        src_label = "✅ Aggiornato via Claude" if indices.get('source') == 'claude_websearch' else \
                    "📦 Cache" if indices.get('source') != 'fallback' else "📦 Valori predefiniti"
        st.caption(src_label)
    else:
        indices = FALLBACK.copy()
        market_offers = {'luce': FALLBACK_OFFERS['luce'], 'gas': FALLBACK_OFFERS['gas'], 'source': 'fallback'}
        st.caption("⚠️ API key non impostata — usando valori predefiniti")

    col1, col2 = st.columns(2)
    with col1:
        st.metric("PUN", f"{indices['PUN_Luce']:.4f}", "€/kWh")
    with col2:
        st.metric("PSV", f"{indices['PSV_Gas']:.4f}", "€/Smc")

    if st.button("🔄 Forza aggiornamento"):
        cache_file = os.path.join(os.path.dirname(__file__), ".market_cache.json")
        if os.path.exists(cache_file):
            os.remove(cache_file)
        st.rerun()

    st.markdown("---")
    st.markdown("### 🏢 Aggiungi Offerta Personalizzata")
    custom_name = st.text_input("Nome gestore", "La mia offerta")
    custom_type = st.selectbox("Tipo", ["Variabile", "Fisso"])
    if custom_type == "Variabile":
        cl_spread = st.number_input("Spread Luce (€/kWh)", value=0.010, format="%.4f")
        cg_spread = st.number_input("Spread Gas (€/Smc)",  value=0.038, format="%.4f")
        cl_fixed  = st.number_input("Quota fissa Luce (€/mese)", value=10.0)
        cg_fixed  = st.number_input("Quota fissa Gas (€/mese)",  value=10.0)
        custom_offer = {'name': custom_name, 'type': 'Variabile',
                        'luce_spread': cl_spread, 'gas_spread': cg_spread,
                        'luce_fixed': cl_fixed, 'gas_fixed': cg_fixed, 'bonus': 0}
    else:
        cl_price = st.number_input("Prezzo fisso Luce (€/kWh)", value=0.110, format="%.4f")
        cg_price = st.number_input("Prezzo fisso Gas (€/Smc)",  value=0.450, format="%.4f")
        cl_fixed = st.number_input("Quota fissa Luce (€/mese)", value=10.0)
        cg_fixed = st.number_input("Quota fissa Gas (€/mese)",  value=10.0)
        custom_offer = {'name': custom_name, 'type': 'Fisso',
                        'luce_price': cl_price, 'gas_price': cg_price,
                        'luce_fixed': cl_fixed, 'gas_fixed': cg_fixed, 'bonus': 0}

    add_custom = st.button("➕ Aggiungi al confronto")
    if add_custom:
        st.session_state.setdefault('custom_offers', []).append(custom_offer)
        st.success(f"Offerta '{custom_name}' aggiunta!")

    st.markdown("---")
    st.caption("CLEM v1.0 — Powered by Claude & Streamlit")


extra_offers = st.session_state.get('custom_offers', [])

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab_personal, tab_guest = st.tabs([
    "🏠 La Mia Analisi (PDF bollette)",
    "👥 Analisi Rapida (Ospite)"
])


# ═══════════════════════════════════════════════════════════════════════════════
#  TAB 1 — ANALISI PERSONALE
# ═══════════════════════════════════════════════════════════════════════════════
with tab_personal:

    BILLS_DIR = os.path.expanduser("~/CLEM_bills")
    os.makedirs(BILLS_DIR, exist_ok=True)

    col_up, col_arch = st.columns([2, 1])
    with col_up:
        st.markdown("### 📥 Carica Bollette")
        uploaded = st.file_uploader(
            "Trascina i PDF delle tue bollette (Edison, Enel, Eni Plenitude, A2A, Illumia, Hera, Iren, Acea...)",
            type="pdf", accept_multiple_files=True, key="personal_upload"
        )
        if uploaded:
            for f in uploaded:
                with open(os.path.join(BILLS_DIR, f.name), "wb") as out:
                    out.write(f.getbuffer())
            st.success(f"✅ {len(uploaded)} file archiviati in {BILLS_DIR}")

    with col_arch:
        local_files = [f for f in os.listdir(BILLS_DIR) if f.lower().endswith(".pdf")]
        st.markdown("### 📁 Archivio")
        st.info(f"**{len(local_files)}** bollette archiviate")
        if local_files:
            with st.expander("Vedi file"):
                for f in local_files:
                    st.caption(f"📄 {f}")
            if st.button("🗑️ Svuota archivio", key="clear_bills"):
                for f in local_files:
                    os.remove(os.path.join(BILLS_DIR, f))
                st.rerun()

    if not local_files:
        st.info("👋 Carica almeno una bolletta per iniziare l'analisi.")
    else:
        # Parsing
        data_list = []
        parse_errors = []
        for filename in local_files:
            try:
                parser = EnergyParser(os.path.join(BILLS_DIR, filename))
                bd = parser.parse()
                bd['filename'] = filename
                data_list.append(bd)
            except Exception as e:
                parse_errors.append(f"{filename}: {e}")

        if parse_errors:
            with st.expander("⚠️ Errori di parsing"):
                for err in parse_errors:
                    st.caption(err)

        if not data_list:
            st.warning("Nessuna bolletta leggibile. Controlla i file caricati.")
        else:
            # Costruzione DataFrame con fillna selettivo
            df = pd.DataFrame(data_list)
            for col in ['consumption', 'spread', 'total_cost', 'price_per_unit']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
            for col in ['bill_date', 'vendor', 'type', 'pod', 'pdr', 'filename']:
                if col in df.columns:
                    df[col] = df[col].fillna('')

            # Fornitore della bolletta più recente
            df['_dp'] = pd.to_datetime(df['bill_date'], format='%d/%m/%Y', errors='coerce')
            most_recent_row = df.sort_values('_dp', ascending=False).iloc[0] if not df.empty else None
            primary_vendor = most_recent_row['vendor'] if most_recent_row is not None and most_recent_row['vendor'] else 'Sconosciuto'

            # Conta bollette per fornitore per il badge
            vendor_counts = df['vendor'].value_counts().to_dict() if 'vendor' in df.columns else {}
            vendor_summary = " | ".join([f"{v}: {n}" for v, n in vendor_counts.items()])

            st.markdown(f"#### Fornitore attuale: **{primary_vendor}** "
                        f"<span class='vendor-badge'>{vendor_summary}</span>",
                        unsafe_allow_html=True)

            # Separazione Luce / Gas
            luce_df = df[df['type'] == 'Luce'] if 'type' in df.columns else pd.DataFrame()
            gas_df  = df[df['type'] == 'Gas']  if 'type' in df.columns else pd.DataFrame()

            current_luce = sort_by_date(luce_df)
            current_gas  = sort_by_date(gas_df)

            # Costi fissi per vendor
            fv = FIXED_COSTS.get(primary_vendor, FIXED_COSTS['Altro'])
            luce_fixed_month = fv['Luce']
            gas_fixed_month  = fv['Gas']

            # ── Storico consumi ───────────────────────────────────────────────────────
            if len(luce_df) > 1 or len(gas_df) > 1:
                st.markdown("---")
                st.markdown("### 📊 Storico Consumi")
                hist_col1, hist_col2 = st.columns(2)
                with hist_col1:
                    if len(luce_df) > 1:
                        st.markdown("**⚡ Energia Elettrica (kWh)**")
                        hist_l = luce_df[['bill_date','consumption']].copy()
                        hist_l['_dp'] = pd.to_datetime(hist_l['bill_date'], format='%d/%m/%Y', errors='coerce')
                        hist_l = hist_l.sort_values('_dp').set_index('bill_date')['consumption']
                        st.bar_chart(hist_l)
                with hist_col2:
                    if len(gas_df) > 1:
                        st.markdown("**🔥 Gas Naturale (Smc)**")
                        hist_g = gas_df[['bill_date','consumption']].copy()
                        hist_g['_dp'] = pd.to_datetime(hist_g['bill_date'], format='%d/%m/%Y', errors='coerce')
                        hist_g = hist_g.sort_values('_dp').set_index('bill_date')['consumption']
                        st.bar_chart(hist_g)

            # ── Analisi tecnica ───────────────────────────────────────────────────────
            st.markdown("---")
            st.markdown("### 🔬 Analisi Tecnica")
            col_l, col_g = st.columns(2)
            with col_l:
                if current_luce is not None:
                    st.markdown("#### ⚡ Energia Elettrica")
                    st.caption(f"Bolletta del {current_luce.get('bill_date','—')} | Fornitore: {current_luce.get('vendor','—')}")
                    cons_l = safe_float(current_luce.get('consumption', 0))
                    days_l = int(safe_float(current_luce.get('billing_days', 0)))
                    ptype_l = current_luce.get('period_type', 'stimato')
                    annual_l, mult_l, fonte_l = annual_projection(cons_l, days_l, ptype_l)
                    st.table(pd.DataFrame([
                        {"Voce": "PUN (Mercato)",       "Valore": f"~{indices['PUN_Luce']} €/kWh",  "Note": "Aggiornato"},
                        {"Voce": "Perdite di rete",     "Valore": "+10% sul PUN",                      "Note": "Moltiplicatore 1,10"},
                        {"Voce": "Spread contratto",    "Valore": f"{safe_float(current_luce.get('spread',0)):.4f} €/kWh", "Note": "Da bolletta"},
                        {"Voce": "Quota fissa",         "Valore": f"{luce_fixed_month:.2f} €/mese",  "Note": "PCV + Potenza"},
                        {"Voce": "Consumo periodo",     "Valore": f"{cons_l:.0f} kWh",                 "Note": fonte_l},
                        {"Voce": "Proiezione annua",    "Valore": f"{annual_l:.0f} kWh/anno",          "Note": f"Moltiplicatore {mult_l}"},
                    ]))
            with col_g:
                if current_gas is not None:
                    st.markdown("#### 🔥 Gas Naturale")
                    st.caption(f"Bolletta del {current_gas.get('bill_date','—')} | Fornitore: {current_gas.get('vendor','—')}")
                    cons_g = safe_float(current_gas.get('consumption', 0))
                    days_g = int(safe_float(current_gas.get('billing_days', 0)))
                    ptype_g = current_gas.get('period_type', 'stimato')
                    annual_g, mult_g, fonte_g = annual_projection(cons_g, days_g, ptype_g)
                    st.table(pd.DataFrame([
                        {"Voce": "PSV (Mercato)",       "Valore": f"{indices['PSV_Gas']} €/Smc",     "Note": "Aggiornato"},
                        {"Voce": "Spread contratto",    "Valore": f"{safe_float(current_gas.get('spread',0)):.4f} €/Smc", "Note": "Da bolletta"},
                        {"Voce": "Quota fissa",         "Valore": f"{gas_fixed_month:.2f} €/mese",   "Note": "QVD + Quote"},
                        {"Voce": "Consumo periodo",     "Valore": f"{cons_g:.0f} Smc",                 "Note": fonte_g},
                        {"Voce": "Proiezione annua",    "Valore": f"{annual_g:.0f} Smc/anno",          "Note": f"Moltiplicatore {mult_g}"},
                    ]))

            # ── Simulazione risparmio ─────────────────────────────────────────────────
            if current_luce is not None or current_gas is not None:
                st.markdown("---")
                st.markdown("### 💰 Simulazione Risparmio")

                luce_cons   = safe_float(current_luce.get('consumption', 352)) if current_luce is not None else 0.0
                gas_cons    = safe_float(current_gas.get('consumption', 194))  if current_gas  is not None else 0.0
                luce_days   = int(safe_float(current_luce.get('billing_days', 0))) if current_luce is not None else 0
                gas_days    = int(safe_float(current_gas.get('billing_days', 0)))  if current_gas  is not None else 0
                luce_ptype  = current_luce.get('period_type', 'stimato') if current_luce is not None else 'stimato'
                gas_ptype   = current_gas.get('period_type', 'stimato')  if current_gas  is not None else 'stimato'

                luce_kwh, _, _ = annual_projection(luce_cons, luce_days, luce_ptype)
                gas_smc,  _, _ = annual_projection(gas_cons,  gas_days,  gas_ptype)
                luce_spread = safe_float(current_luce.get('spread', 0)) if current_luce is not None else 0.0
                gas_spread  = safe_float(current_gas.get('spread', 0))  if current_gas  is not None else 0.0

                annual_cost = compute_annual_cost(
                    luce_kwh, gas_smc, indices,
                    luce_spread, gas_spread,
                    luce_fixed_month, gas_fixed_month
                )

                res_df, best_offer, best_saving = render_simulation_tabs(
                    annual_cost, luce_kwh, gas_smc,
                    luce_kwh, gas_smc,
                    luce_spread, gas_spread,
                    luce_fixed_month, gas_fixed_month,
                    indices, market_offers, extra_offers
                )

                # Share link
                render_share_link(luce_kwh, gas_smc, luce_spread, gas_spread,
                                  luce_fixed_month, gas_fixed_month, primary_vendor)

                # PDF
                luce_dict = current_luce.to_dict() if current_luce is not None else {}
                gas_dict  = current_gas.to_dict()  if current_gas  is not None else {}
                luce_dict['fixed_cost'] = luce_fixed_month
                gas_dict['fixed_cost']  = gas_fixed_month
                pdf_bytes = generate_pdf_report(primary_vendor, luce_dict, gas_dict,
                                                res_df, indices, annual_cost)
                st.download_button(
                    "📥 Scarica Report PDF",
                    data=pdf_bytes,
                    file_name=f"CLEM_Report_{date.today()}.pdf",
                    mime="application/pdf"
                )
            else:
                st.info("Carica almeno una bolletta per la simulazione.")



# ═══════════════════════════════════════════════════════════════════════════════
#  TAB 2 — ANALISI OSPITE
# ═══════════════════════════════════════════════════════════════════════════════
with tab_guest:
    st.markdown("### 👥 Analisi Rapida — Ospite")
    st.caption("Nessun account necessario. Carica la tua bolletta oppure inserisci i dati manualmente.")

    prefill = get_prefill()

    guest_mode = st.radio(
        "Come vuoi procedere?",
        ["📄 Carico la mia bolletta (PDF)", "⌨️ Inserisco i dati manualmente"],
        horizontal=True, key="guest_mode"
    )

    guest_vendor    = prefill.get('vendor', 'Altro')
    guest_luce_kwh  = prefill.get('luce', 300.0)
    guest_gas_smc   = prefill.get('gas',  150.0)
    guest_luce_spr  = prefill.get('ls', 0.0)
    guest_gas_spr   = prefill.get('gs', 0.0)
    guest_luce_fix  = prefill.get('lf', 15.0)
    guest_gas_fix   = prefill.get('gf', 13.0)

    if guest_mode == "📄 Carico la mia bolletta (PDF)":
        guest_file = st.file_uploader("Carica la tua bolletta PDF", type="pdf", key="guest_pdf")
        if guest_file:
            try:
                parser = EnergyParser(io.BytesIO(guest_file.read()))
                bd = parser.parse()
                guest_vendor   = bd.get('vendor', 'Altro')
                guest_luce_kwh = safe_float(bd.get('consumption', 300)) if bd.get('type') == 'Luce' else guest_luce_kwh
                guest_gas_smc  = safe_float(bd.get('consumption', 150)) if bd.get('type') == 'Gas'  else guest_gas_smc
                guest_luce_spr = safe_float(bd.get('spread', 0))        if bd.get('type') == 'Luce' else guest_luce_spr
                guest_gas_spr  = safe_float(bd.get('spread', 0))        if bd.get('type') == 'Gas'  else guest_gas_spr
                st.success(f"✅ Bolletta letta — Fornitore: **{guest_vendor}** | Tipo: **{bd.get('type','?')}** | "
                           f"Consumo: **{bd.get('consumption',0)} {'kWh' if bd.get('type')=='Luce' else 'Smc'}**")
                st.info("ℹ️ Hai caricato una sola bolletta. Integra i dati mancanti qui sotto se necessario.")
            except Exception as e:
                st.error(f"Errore lettura PDF: {e}")

    # Form dati (sempre visibile per completare / correggere)
    st.markdown("#### Dati per la simulazione")
    col1, col2, col3 = st.columns(3)
    with col1:
        guest_vendor = st.selectbox(
            "Fornitore attuale",
            list(FIXED_COSTS.keys()),
            index=list(FIXED_COSTS.keys()).index(guest_vendor) if guest_vendor in FIXED_COSTS else 0,
            key="g_vendor"
        )
    with col2:
        guest_luce_kwh = st.number_input("Consumo mensile Luce (kWh)", value=float(guest_luce_kwh), min_value=0.0, key="g_luce")
    with col3:
        guest_gas_smc = st.number_input("Consumo mensile Gas (Smc)", value=float(guest_gas_smc), min_value=0.0, key="g_gas")

    col4, col5, col6, col7 = st.columns(4)
    with col4:
        guest_luce_spr = st.number_input("Spread Luce (€/kWh)", value=float(guest_luce_spr), format="%.4f", key="g_lspr",
                                          help="Lascia 0 se non lo conosci — verrà usato solo il PUN")
    with col5:
        guest_gas_spr = st.number_input("Spread Gas (€/Smc)", value=float(guest_gas_spr), format="%.4f", key="g_gspr")
    with col6:
        fv_g = FIXED_COSTS.get(guest_vendor, FIXED_COSTS['Altro'])
        guest_luce_fix = st.number_input("Quota fissa Luce (€/mese)", value=float(guest_luce_fix) or fv_g['Luce'], key="g_lfix")
    with col7:
        guest_gas_fix = st.number_input("Quota fissa Gas (€/mese)", value=float(guest_gas_fix) or fv_g['Gas'], key="g_gfix")

    if st.button("🔍 Calcola risparmio", type="primary", key="guest_calc"):
        if guest_luce_kwh == 0 and guest_gas_smc == 0:
            st.error("Inserisci almeno un consumo (Luce o Gas) per procedere.")
        else:
            annual_luce = guest_luce_kwh * 12
            annual_gas  = guest_gas_smc  * 12

            guest_annual = compute_annual_cost(
                annual_luce, annual_gas, indices,
                guest_luce_spr, guest_gas_spr,
                guest_luce_fix, guest_gas_fix
            )

            st.markdown("---")
            st.metric("💶 Spesa Annua Stimata (fornitore attuale)", f"€ {guest_annual:,.2f}")

            res_df_g, best_offer_g, best_saving_g = render_comparison_table(
                guest_annual, annual_luce, annual_gas, indices, extra_offers
            )
            render_alert(best_offer_g, best_saving_g)

            render_share_link(annual_luce, annual_gas, guest_luce_spr, guest_gas_spr,
                              guest_luce_fix, guest_gas_fix, guest_vendor)

            # PDF ospite
            pdf_g = generate_pdf_report(
                guest_vendor,
                {'spread': guest_luce_spr, 'consumption': annual_luce, 'fixed_cost': guest_luce_fix},
                {'spread': guest_gas_spr,  'consumption': annual_gas,  'fixed_cost': guest_gas_fix},
                res_df_g, indices, guest_annual
            )
            st.download_button(
                "📥 Scarica Report PDF",
                data=pdf_g,
                file_name=f"CLEM_Analisi_Ospite_{date.today()}.pdf",
                mime="application/pdf"
            )
